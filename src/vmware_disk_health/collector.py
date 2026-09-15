"""Collect disk health from ESXi hosts.

Only tools that ship with ESXi are required: ``esxcli storage core device smart
get`` for every disk and ``esxcli nvme device log smart get`` for NVMe. If the
community smartctl VIB is installed it adds full ATA attribute data; it is
never assumed to be present. Everything run on the host is read-only.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import time
from collections.abc import Awaitable, Callable

from .config import HostConfig, Settings
from .evaluate import evaluate
from .model import DiskInfo, DiskKind, DiskResult, HostResult, Reading
from .parsers import esxcli
from .parsers.smartctl import SmartctlError, parse_smartctl
from .transport import Transport

log = logging.getLogger(__name__)

SMARTCTL_CANDIDATES = ("/opt/smartmontools/smartctl",)
PER_HOST_CONCURRENCY = 3

Connect = Callable[[HostConfig], Awaitable[Transport]]
PreviousReading = Callable[[str], Reading | None]


class CommandFailed(Exception):
    pass


class HostCollector:
    def __init__(self, host: HostConfig, transport: Transport, settings: Settings):
        self.host = host
        self.transport = transport
        self.settings = settings
        self._semaphore = asyncio.Semaphore(PER_HOST_CONCURRENCY)

    async def run(self, command: str, *, required: bool = False) -> str | None:
        async with self._semaphore:
            try:
                result = await self.transport.run(command, timeout=self.settings.command_timeout_seconds)
            except TimeoutError as exc:
                if required:
                    raise CommandFailed(f"`{command}` timed out") from exc
                log.warning("%s: `%s` timed out", self.host.name, command)
                return None
        if not result.ok:
            message = (result.stderr or result.stdout).strip().splitlines()
            detail = message[-1] if message else f"exit status {result.exit_status}"
            if required:
                raise CommandFailed(f"`{command}` failed: {detail}")
            log.debug("%s: `%s` failed: %s", self.host.name, command, detail)
            return None
        return result.stdout

    async def find_smartctl(self) -> str | None:
        setting = self.host.smartctl_path.strip()
        if setting.lower() in {"none", "off", "false", ""}:
            return None
        candidates = SMARTCTL_CANDIDATES if setting.lower() == "auto" else (setting,)
        for path in candidates:
            if await self.run(f"test -x {shlex.quote(path)}") is not None:
                return path
        return None

    async def nvme_adapters(self, disks: list[DiskInfo]) -> None:
        """Attach the vmhba adapter to each NVMe disk; its SMART log is queried per adapter."""
        nvme = [d for d in disks if d.kind is DiskKind.NVME]
        if not nvme:
            return
        paths = esxcli.parse_path_list(await self.run("esxcli storage core path list") or "")
        adapters = esxcli.parse_nvme_adapters(await self.run("esxcli nvme device list") or "")
        for disk in nvme:
            disk.nvme_adapter = paths.get(disk.device_id)
        unmapped = [d for d in nvme if not d.nvme_adapter]
        if len(unmapped) == 1 and len(adapters) == len(nvme):
            taken = {d.nvme_adapter for d in nvme}
            free = [a for a in adapters if a not in taken]
            if len(free) == 1:
                unmapped[0].nvme_adapter = free[0]

    async def nvme_identity(self, disk: DiskInfo, reading_out: dict[str, Reading]) -> None:
        if not disk.nvme_adapter:
            return
        props = esxcli.parse_nvme_device_get(await self.run(f"esxcli nvme device get -A {shlex.quote(disk.nvme_adapter)}") or "")
        for key, value in props.items():
            lowered = key.lower()
            if lowered.startswith("serial number") and value:
                disk.serial = value.strip()
            elif lowered.startswith("warning composite temperature threshold"):
                limit = esxcli.kelvin_to_celsius(value)
                if limit:
                    reading_out[disk.device_id] = Reading(temperature_limit_c=limit)

    async def collect_disk(self, disk: DiskInfo, smartctl: str | None, extra: Reading | None) -> DiskResult:
        result = DiskResult(host=self.host.name, info=disk)
        readings: list[Reading] = []  # highest priority first
        device = shlex.quote(disk.device_id)

        if smartctl and disk.kind is not DiskKind.NVME:
            output = await self.run_allow_status(f"{shlex.quote(smartctl)} -j -x -n standby -d sat,auto /dev/disks/{device}")
            if output:
                try:
                    reading, raw = parse_smartctl(output)
                except SmartctlError as exc:
                    result.errors.append(f"smartctl: {exc}")
                else:
                    readings.append(reading)
                    result.sources.append("smartctl")
                    result.raw["smartctl"] = raw
                    disk.model = raw.get("model_name") or disk.model
                    disk.serial = raw.get("serial_number") or disk.serial
                    disk.firmware = raw.get("firmware_version") or disk.firmware

        if disk.kind is DiskKind.NVME:
            if disk.nvme_adapter:
                output = await self.run(f"esxcli nvme device log smart get -A {shlex.quote(disk.nvme_adapter)}")
                if output:
                    reading, raw = esxcli.parse_nvme_smart_log(output)
                    readings.append(reading)
                    result.sources.append("esxcli-nvme")
                    result.raw["esxcli_nvme"] = raw
                else:
                    result.errors.append(f"NVMe SMART log unavailable on {disk.nvme_adapter}")
            else:
                result.errors.append("could not map NVMe device to its adapter")

        output = await self.run(f"esxcli storage core device smart get -d {device}")
        if output:
            reading, raw = esxcli.parse_native_smart(output)
            readings.append(reading)
            result.sources.append("esxcli-smart")
            result.raw["esxcli_smart"] = raw
        else:
            result.errors.append("esxcli SMART data unavailable (drive or controller may not support it)")

        if extra:
            readings.append(extra)
        merged = Reading()
        for reading in readings:
            merged = merged.merge(reading)
        result.reading = merged
        return result

    async def run_allow_status(self, command: str) -> str | None:
        """Like run(), but keeps stdout for tools whose exit status is a bitmask."""
        async with self._semaphore:
            try:
                result = await self.transport.run(command, timeout=self.settings.command_timeout_seconds)
            except TimeoutError:
                log.warning("%s: `%s` timed out", self.host.name, command)
                return None
        return result.stdout or None

    async def collect(self, previous: PreviousReading) -> HostResult:
        started = time.time()
        host_result = HostResult(name=self.host.name, address=self.host.address, collected_at=started)
        host_result.esxi_version = esxcli.parse_version(await self.run("vmware -vl") or "")

        disks = esxcli.parse_device_list(await self.run("esxcli storage core device list", required=True) or "")
        disks = [d for d in disks if not self.settings.is_excluded(self.host, d)]
        await self.nvme_adapters(disks)
        extras: dict[str, Reading] = {}
        await asyncio.gather(*(self.nvme_identity(d, extras) for d in disks if d.kind is DiskKind.NVME))
        smartctl = await self.find_smartctl()
        host_result.smartctl_path = smartctl

        results = await asyncio.gather(*(self.collect_disk(d, smartctl, extras.get(d.device_id)) for d in disks))
        for result in results:
            evaluate(result, self.settings.thresholds_for(result.info), previous(result.key))
        host_result.disks = list(results)
        host_result.ok = True
        host_result.duration_s = round(time.time() - started, 2)
        return host_result


async def collect_host(host: HostConfig, settings: Settings, connect: Connect, previous: PreviousReading) -> HostResult:
    started = time.time()
    transport: Transport | None = None
    try:
        transport = await connect(host)
        return await HostCollector(host, transport, settings).collect(previous)
    except Exception as exc:  # noqa: BLE001 - one broken host must never stop the others
        log.warning("%s: collection failed: %s", host.name, exc)
        return HostResult(
            name=host.name,
            address=host.address,
            ok=False,
            error=str(exc) or exc.__class__.__name__,
            collected_at=started,
            duration_s=round(time.time() - started, 2),
        )
    finally:
        if transport:
            try:
                await transport.close()
            except Exception:  # noqa: BLE001 - closing a dead connection is best effort
                pass


async def collect_all(settings: Settings, connect: Connect, previous: PreviousReading) -> list[HostResult]:
    hosts = [h for h in settings.hosts if h.enabled]
    return list(await asyncio.gather(*(collect_host(h, settings, connect, previous) for h in hosts)))
