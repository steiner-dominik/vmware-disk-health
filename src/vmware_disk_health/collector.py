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
from typing import Any

from .config import HostConfig, Settings
from .evaluate import evaluate
from .model import DiskInfo, DiskKind, DiskResult, HostResult, Reading
from .parsers import esxcli
from .parsers.esxcli import Shape
from .parsers.smartctl import SmartctlError, parse_smartctl
from .transport import Transport

log = logging.getLogger(__name__)

SMARTCTL_CANDIDATES = ("/opt/smartmontools/smartctl",)
PER_HOST_CONCURRENCY = 3
# Undocumented, but the only way to get JSON out of esxcli on ESXi 8.
ESXCLI_JSON = "esxcli --debug --formatter=json"

Connect = Callable[[HostConfig], Awaitable[Transport]]
PreviousReading = Callable[[str], Reading | None]


class CommandFailed(Exception):
    pass


class HostCollector:
    def __init__(self, host: HostConfig, transport: Transport, settings: Settings):
        self.host = host
        self.transport = transport
        self.settings = settings
        self.json = False
        self._semaphore = asyncio.Semaphore(PER_HOST_CONCURRENCY)

    async def _exec(self, command: str) -> tuple[str, int] | None:
        async with self._semaphore:
            try:
                result = await self.transport.run(command, timeout=self.settings.command_timeout_seconds)
            except TimeoutError:
                log.warning("%s: `%s` timed out", self.host.name, command)
                return None
        if not result.ok:
            lines = (result.stderr or result.stdout).strip().splitlines()
            log.debug("%s: `%s` exited %s: %s", self.host.name, command, result.exit_status, lines[-1:])
        return result.stdout, result.exit_status

    async def run(self, command: str) -> str | None:
        """stdout of a successful command, None on failure or timeout."""
        result = await self._exec(command)
        return result[0] if result and result[1] == 0 else None

    async def esxcli(self, args: str, shape: Shape, *, required: bool = False) -> Any:
        """Run an esxcli namespace as JSON when the host supports it, else as text."""
        if self.json:
            output = await self.run(f"{ESXCLI_JSON} {args}")
            if output is not None:
                try:
                    return esxcli.decode_json(output, shape)
                except ValueError as exc:
                    log.info("%s: unexpected JSON from `esxcli %s` (%s), using text output", self.host.name, args, exc)
        output = await self.run(f"esxcli {args}")
        if output is None:
            if required:
                raise CommandFailed(f"`esxcli {args}` failed")
            return {} if shape is Shape.RECORD else []
        return esxcli.decode_text(output, shape)

    async def detect(self, host_result: HostResult) -> None:
        output = await self.run(f"{ESXCLI_JSON} system version get")
        if output is not None:
            try:
                host_result.esxi_version = esxcli.parse_version(esxcli.decode_json(output, Shape.RECORD))
                self.json = True
            except ValueError:
                pass
        if not self.json:
            host_result.esxi_version = esxcli.parse_vmware_vl(await self.run("vmware -vl") or "")
        host_result.esxcli_json = self.json

    async def find_smartctl(self) -> str | None:
        setting = self.host.smartctl_path.strip()
        if setting.lower() in {"none", "off", "false", ""}:
            return None
        candidates = SMARTCTL_CANDIDATES if setting.lower() == "auto" else (setting,)
        for path in candidates:
            if await self.run(f"test -x {shlex.quote(path)}") is not None:
                return path
        return None

    async def discover(self) -> list[DiskInfo]:
        records = await self.esxcli("storage core device list", Shape.BLOCKS, required=True)
        disks = [d for d in esxcli.parse_device_list(records) if not self.settings.is_excluded(self.host, d)]
        capacities = esxcli.parse_capacity_list(await self.esxcli("storage core device capacity list", Shape.TABLE))
        for disk in disks:
            disk.logical_block_size, disk.format_type = capacities.get(disk.device_id, (None, ""))
        nvme = [d for d in disks if d.kind is DiskKind.NVME]
        if nvme:
            paths = esxcli.parse_path_list(await self.esxcli("storage core path list", Shape.BLOCKS))
            for disk in nvme:
                disk.nvme_adapter = paths.get(disk.device_id)
        return disks

    async def nvme_identity(self, disk: DiskInfo) -> Reading | None:
        """Fill in the serial number and return the drive's temperature limit."""
        if not disk.nvme_adapter:
            return None
        record = await self.esxcli(f"nvme device get -A {shlex.quote(disk.nvme_adapter)}", Shape.RECORD)
        serial, limit = esxcli.parse_nvme_device_get(record)
        disk.serial = serial or disk.serial
        return Reading(temperature_limit_c=limit) if limit else None

    async def smartctl_reading(self, disk: DiskInfo, smartctl: str, result: DiskResult) -> Reading | None:
        # The exit status is a bitmask; the JSON is still usable for most non-zero values.
        executed = await self._exec(
            f"{shlex.quote(smartctl)} -j -x -n standby -d sat,auto /dev/disks/{shlex.quote(disk.device_id)}"
        )
        if not executed or not executed[0]:
            result.errors.append("smartctl returned no output")
            return None
        try:
            reading, raw = parse_smartctl(executed[0])
        except SmartctlError as exc:
            result.errors.append(f"smartctl: {exc}")
            return None
        result.sources.append("smartctl")
        result.raw["smartctl"] = raw
        disk.model = raw.get("model_name") or disk.model
        disk.serial = raw.get("serial_number") or disk.serial
        disk.firmware = raw.get("firmware_version") or disk.firmware
        return reading

    async def nvme_reading(self, disk: DiskInfo, result: DiskResult) -> list[Reading]:
        readings = []
        identity = await self.nvme_identity(disk)
        if not disk.nvme_adapter:
            result.errors.append("could not map NVMe device to its adapter")
        else:
            record = await self.esxcli(f"nvme device log smart get -A {shlex.quote(disk.nvme_adapter)}", Shape.RECORD)
            if record:
                readings.append(esxcli.parse_nvme_smart_log(record))
                result.sources.append("esxcli-nvme")
                result.raw["esxcli_nvme"] = record
            else:
                result.errors.append(f"NVMe SMART log unavailable on {disk.nvme_adapter}")
        if identity:
            readings.append(identity)
        return readings

    async def collect_disk(self, disk: DiskInfo, smartctl: str | None) -> DiskResult:
        result = DiskResult(host=self.host.name, info=disk)
        readings: list[Reading] = []  # highest priority first

        if smartctl and disk.kind is not DiskKind.NVME:
            if reading := await self.smartctl_reading(disk, smartctl, result):
                readings.append(reading)
        if disk.kind is DiskKind.NVME:
            readings += await self.nvme_reading(disk, result)

        rows = await self.esxcli(f"storage core device smart get -d {shlex.quote(disk.device_id)}", Shape.TABLE)
        if rows:
            readings.append(esxcli.parse_native_smart(rows, disk.kind, disk.logical_block_size))
            result.sources.append("esxcli-smart")
            result.raw["esxcli_smart"] = rows
        else:
            result.errors.append("esxcli SMART data unavailable (drive or controller may not support it)")

        merged = Reading()
        for reading in readings:
            merged = merged.merge(reading)
        result.reading = merged
        return result

    async def collect(self, previous: PreviousReading) -> HostResult:
        started = time.time()
        host_result = HostResult(name=self.host.name, address=self.host.address, collected_at=started)
        await self.detect(host_result)
        disks = await self.discover()
        host_result.smartctl_path = await self.find_smartctl()
        results = await asyncio.gather(*(self.collect_disk(d, host_result.smartctl_path) for d in disks))
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
