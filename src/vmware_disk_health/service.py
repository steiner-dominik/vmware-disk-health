"""The long-running part: periodic collection, persistence and change notifications."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import asyncssh

from .alerts import Notifier
from .collector import Connect, HostCollector, collect_all
from .config import HostConfig, Settings
from .evaluate import RISING_WINDOW_DAYS
from .model import HostResult
from .mqtt import MqttPublisher, supervisor_broker
from .storage import StatusChange, Storage
from .transport import KeyStore, SSHTransport, host_id

log = logging.getLogger(__name__)

Listener = Callable[[HostResult, list[StatusChange]], Awaitable[None]]


class Monitor:
    def __init__(self, settings: Settings, storage: Storage, keys: KeyStore, connect: Connect | None = None):
        self.settings = settings
        self.storage = storage
        self.keys = keys
        self.connect = connect or (lambda host: SSHTransport.connect(host, keys))
        self.listeners: list[Listener] = []
        self.polling = False
        self.last_poll_started: float | None = None
        self.last_poll_finished: float | None = None
        self.next_poll_at: float | None = None
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._last_rollup = 0.0

    # -- lifecycle

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    @property
    def healthy(self) -> bool:
        """The scheduler is alive. Unreachable hosts are data, not an app failure."""
        return self._task is not None and not self._task.done()

    async def _run(self) -> None:
        interval = self.settings.poll_interval_minutes * 60
        while True:
            try:
                await self.poll()
                if time.time() - self._last_rollup > 86400:
                    folded = await asyncio.to_thread(self.storage.rollup, self.settings.retention_raw_days)
                    self._last_rollup = time.time()
                    if folded:
                        log.info("rolled up %d samples older than %d days", folded, self.settings.retention_raw_days)
            except Exception:  # noqa: BLE001 - keep the scheduler alive whatever a poll does
                log.exception("poll failed")
            self.next_poll_at = time.time() + interval
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
            except TimeoutError:
                pass

    def trigger(self) -> bool:
        """Request a poll now; False if one is already running."""
        if self.polling:
            return False
        self._wake.set()
        return True

    # -- work

    async def poll(self) -> list[HostResult]:
        async with self._lock:
            self.polling = True
            self.last_poll_started = time.time()
            try:
                since = time.time() - RISING_WINDOW_DAYS * 86400
                results = await collect_all(self.settings, self.connect, lambda key: self.storage.readings_before(key, since))
                for host in results:
                    changes = self.storage.save(host)
                    for change in changes:
                        log.info(
                            "%s %s: %s -> %s",
                            host.name,
                            change.disk.info.model,
                            change.old and change.old.label,
                            change.new.label,
                        )
                    for listener in self.listeners:
                        try:
                            await listener(host, changes)
                        except Exception:  # noqa: BLE001 - a broken integration must not stop collection
                            log.exception("listener %r failed", listener)
                return results
            finally:
                self.polling = False
                self.last_poll_finished = time.time()

    async def test_host(self, host: HostConfig) -> dict:
        """Connect and report what the app can see, without collecting disk data."""
        started = time.time()
        transport = None
        try:
            transport = await self.connect(host)
            collector = HostCollector(host, transport, self.settings)
            probe = HostResult(name=host.name, address=host.address)
            await collector.detect(probe)
            return {
                "ok": True,
                "esxi_version": probe.esxi_version,
                "esxcli_json": probe.esxcli_json,
                "smartctl_path": await collector.find_smartctl(),
                "host_key": self.fingerprint(host),
                "duration_s": round(time.time() - started, 2),
            }
        except Exception as exc:  # noqa: BLE001 - reported to the user verbatim
            return {"ok": False, "error": str(exc) or exc.__class__.__name__, "host_key": self.fingerprint(host)}
        finally:
            if transport:
                await transport.close()

    def fingerprint(self, host: HostConfig) -> str | None:
        pinned = self.keys.pinned(host_id(host))
        if not pinned:
            return None
        try:
            return asyncssh.import_public_key(pinned).get_fingerprint()
        except (asyncssh.KeyImportError, ValueError):
            return None


class Integrations:
    """Optional outputs: Home Assistant entities over MQTT, and notifications."""

    def __init__(self, settings: Settings, monitor: Monitor):
        self.settings = settings
        self.monitor = monitor
        self.publisher: MqttPublisher | None = None
        self.notifier: Notifier | None = None
        self.mqtt_error: str | None = None

    async def start(self) -> None:
        config = self.settings.mqtt
        if config.enabled:
            if not config.host:
                # As a Home Assistant app, ask the Supervisor for the broker.
                discovered = await asyncio.to_thread(supervisor_broker)
                if discovered:
                    config = discovered.model_copy(
                        update={"discovery_prefix": config.discovery_prefix, "base_topic": config.base_topic}
                    )
            if config.host:
                self.publisher = MqttPublisher(self.settings, config)
                try:
                    await self.publisher.start()
                    self.monitor.listeners.append(self.publisher.publish)
                except Exception as exc:  # noqa: BLE001 - a broken broker must not stop monitoring
                    self.mqtt_error = str(exc)
                    log.warning("MQTT disabled: %s", exc)
                    self.publisher = None
            else:
                self.mqtt_error = "no broker configured"
        notifier = Notifier(self.settings.alerts)
        if notifier.configured:
            self.notifier = notifier
            self.monitor.listeners.append(notifier.on_results)

    async def stop(self) -> None:
        if self.publisher:
            await self.publisher.stop()

    def status(self) -> dict:
        client = self.publisher.client if self.publisher else None
        return {
            "mqtt_enabled": self.settings.mqtt.enabled,
            "mqtt_connected": bool(client and client.is_connected()),
            "mqtt_broker": f"{self.publisher.config.host}:{self.publisher.config.port}" if self.publisher else None,
            "mqtt_error": self.mqtt_error,
            "alerts_enabled": bool(self.notifier),
        }
