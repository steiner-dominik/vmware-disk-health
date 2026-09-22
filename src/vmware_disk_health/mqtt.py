"""Home Assistant entities through MQTT discovery.

Every disk becomes one HA device (linked to its ESXi host device) with a
status, a problem binary sensor and one sensor per value the drive reports.
Discovery configs and states are retained, so entities survive a restart of
either side. Building the messages is kept free of I/O so it can be tested.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime

import paho.mqtt.client as mqtt

from . import __version__
from .config import MqttConfig, Settings
from .model import DiskKind, DiskResult, HostResult, Severity

log = logging.getLogger(__name__)

SUPERVISOR_MQTT_URL = "http://supervisor/services/mqtt"
STATUS_OPTIONS = [s.label for s in Severity]


@dataclass(frozen=True)
class Entity:
    key: str  # unique id suffix and JSON field
    name: str
    component: str = "sensor"
    device_class: str | None = None
    unit: str | None = None
    state_class: str | None = None
    icon: str | None = None
    diagnostic: bool = True


DISK_ENTITIES = (
    Entity("status", "Status", device_class="enum", icon="mdi:harddisk", diagnostic=False),
    Entity("problem", "Problem", component="binary_sensor", device_class="problem", diagnostic=False),
    Entity("temperature_c", "Temperature", device_class="temperature", unit="°C", state_class="measurement"),
    Entity("life_remaining_pct", "Life remaining", unit="%", state_class="measurement", icon="mdi:battery-heart-variant"),
    Entity("life_used_pct", "Endurance used", unit="%", state_class="measurement", icon="mdi:chart-donut"),
    Entity("written_bytes", "Data written", device_class="data_size", unit="B", state_class="total_increasing"),
    Entity("read_bytes", "Data read", device_class="data_size", unit="B", state_class="total_increasing"),
    Entity("power_on_hours", "Power-on time", device_class="duration", unit="h", state_class="total_increasing"),
    Entity("power_cycles", "Power cycles", state_class="total_increasing", icon="mdi:power"),
    Entity("unsafe_shutdowns", "Unsafe shutdowns", state_class="total_increasing", icon="mdi:power-plug-off"),
    Entity("reallocated_sectors", "Reallocated sectors", state_class="measurement", icon="mdi:alert-circle-outline"),
    Entity("pending_sectors", "Pending sectors", state_class="measurement", icon="mdi:alert-circle-outline"),
    Entity("offline_uncorrectable", "Offline uncorrectable sectors", state_class="measurement", icon="mdi:alert-circle-outline"),
    Entity(
        "reported_uncorrectable", "Reported uncorrectable errors", state_class="total_increasing", icon="mdi:alert-circle-outline"
    ),
    Entity("crc_errors", "CRC errors", state_class="total_increasing", icon="mdi:cable-data"),
    Entity("media_errors", "Media errors", state_class="total_increasing", icon="mdi:alert-circle-outline"),
    Entity("available_spare_pct", "Available spare", unit="%", state_class="measurement", icon="mdi:gauge"),
)

HOST_ENTITIES = (
    Entity("reachable", "Reachable", component="binary_sensor", device_class="connectivity", diagnostic=False),
    Entity("last_success", "Last successful poll", device_class="timestamp"),
    Entity("disks", "Disks", state_class="measurement", icon="mdi:harddisk"),
    Entity("problem_disks", "Disks with problems", state_class="measurement", icon="mdi:harddisk-remove"),
)


def slug(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]", "_", value.lower())).strip("_")


def short_id(device_id: str) -> str:
    """Last few characters of a device id: readable enough to tell disks apart.

    Used when a disk has no serial number, which happens for drives that
    report their own WWN (esxcli then names them ``naa.*``/``eui.*`` instead
    of a ``t10.ATA_____<model><serial>`` id the serial can be read from).
    """
    tail = re.sub(r"^[a-z0-9]+\.", "", device_id.lower())
    return (tail[-8:] or device_id).upper()


def _config(entity: Entity, unique_prefix: str, state_topic: str, device: dict, base_topic: str) -> dict:
    # No object_id: with has_entity_name, Home Assistant builds the entity id
    # from the device name and the entity name, which is what we want.
    payload = {
        "name": entity.name,
        "unique_id": f"{unique_prefix}_{entity.key}",
        "state_topic": state_topic,
        "value_template": f"{{{{ value_json.{entity.key} }}}}",
        "availability_topic": f"{base_topic}/status",
        "has_entity_name": True,
        "device": device,
    }
    optional = {
        "device_class": entity.device_class,
        "unit_of_measurement": entity.unit,
        "state_class": entity.state_class,
        "icon": entity.icon,
    }
    payload.update({key: value for key, value in optional.items() if value})
    if entity.diagnostic:
        payload["entity_category"] = "diagnostic"
    if entity.device_class == "enum":
        payload["options"] = STATUS_OPTIONS
    if entity.component == "binary_sensor":
        payload.update(payload_on="ON", payload_off="OFF")
    return payload


def host_device(host: HostResult | str, version: str | None = None) -> dict:
    name = host if isinstance(host, str) else host.name
    device = {
        "identifiers": [f"vmware_disk_health_host_{slug(name)}"],
        "name": name,
        "manufacturer": "VMware",
        "model": "ESXi host",
    }
    if version:
        device["sw_version"] = version
    return device


def disk_name(disk: DiskResult, display_name: str | None = None) -> str:
    """Model and serial: several identical models in one host must stay apart.

    Without a serial (WWN-reporting drives esxcli names ``naa.*``/``eui.*``,
    typically without smartctl to read the real one), the model alone would
    collide the same way, so a short id suffix stands in for it instead.
    """
    if display_name:
        return display_name
    info = disk.info
    if info.model and info.serial:
        return f"{info.model} {info.serial}"
    if info.model:
        return f"{info.model} {short_id(disk.key)}"
    return info.serial or disk.key


def disk_unique(disk: DiskResult) -> str:
    """Entity id prefix. The serial keeps it readable; the device id is the fallback."""
    return f"vdh_{slug(disk.info.serial)}" if disk.info.serial else f"vdh_{slug(disk.key)}"


def legacy_disk_unique(disk: DiskResult) -> str:
    """The scheme used up to 26.09.17, whose discovery configs have to be withdrawn."""
    return f"vdh_{slug(disk.key)}"


def disk_device(disk: DiskResult, display_name: str | None = None) -> dict:
    info = disk.info
    return {
        "identifiers": [f"vmware_disk_health_{slug(disk.key)}"],
        "name": disk_name(disk, display_name),
        "manufacturer": (info.vendor or "").strip() or None,
        "model": info.model or None,
        "serial_number": info.serial or None,
        "sw_version": info.firmware or None,
        "via_device": f"vmware_disk_health_host_{slug(disk.host)}",
    }


def disk_state(disk: DiskResult, missing: bool = False) -> dict:
    r = disk.reading
    status = "unknown" if missing else disk.status.label
    state = {
        "status": status,
        "problem": "ON" if status in {"warning", "critical"} else "OFF",
        "life_remaining_pct": r.life_remaining_pct,
        "host": disk.host,
        "device_id": disk.info.device_id,
        "model": disk.info.model,
        "serial": disk.info.serial,
        "kind": disk.info.kind.value,
        "sources": disk.sources,
        "findings": [f.message for f in disk.findings],
        "updated": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    for entity in DISK_ENTITIES:
        if entity.key not in state:
            state[entity.key] = getattr(r, entity.key, None)
    return state


def disk_messages(
    disk: DiskResult, config: MqttConfig, display_name: str | None = None, unique_prefix: str | None = None
) -> list[tuple[str, dict]]:
    """Discovery configs for the values this drive actually reports."""
    base = config.base_topic
    unique = unique_prefix or disk_unique(disk)
    state_topic = f"{base}/disk/{slug(disk.key)}/state"
    device = disk_device(disk, display_name)
    state = disk_state(disk)
    messages = []
    for entity in DISK_ENTITIES:
        if state.get(entity.key) is None:
            continue  # the drive does not report it; no entity that is forever unknown
        if entity.key == "life_used_pct" and disk.info.kind is DiskKind.HDD:
            continue
        payload = _config(entity, unique, state_topic, device, base)
        if entity.key == "status":
            payload["json_attributes_topic"] = state_topic
        messages.append((f"{config.discovery_prefix}/{entity.component}/{unique}/{entity.key}/config", payload))
    return messages


def legacy_disk_topics(disk: DiskResult, config: MqttConfig) -> list[str]:
    """Discovery topics of the pre-26.09.18 entities, so they can be removed."""
    unique = legacy_disk_unique(disk)
    return [f"{config.discovery_prefix}/{e.component}/{unique}/{e.key}/config" for e in DISK_ENTITIES]


def host_state(host: HostResult, disks: list[DiskResult]) -> dict:
    return {
        "reachable": "ON" if host.ok else "OFF",
        "last_success": datetime.fromtimestamp(host.collected_at, UTC).isoformat(timespec="seconds") if host.ok else None,
        "disks": len(disks),
        "problem_disks": sum(1 for d in disks if d.status >= Severity.WARNING),
        "esxi_version": host.esxi_version,
        "smartctl": host.smartctl_path,
        "duration_s": host.duration_s,
        "error": host.error,
    }


def host_messages(host: HostResult, config: MqttConfig) -> list[tuple[str, dict]]:
    base = config.base_topic
    unique = f"vdh_host_{slug(host.name)}"
    state_topic = f"{base}/host/{slug(host.name)}/state"
    device = host_device(host, host.esxi_version)
    messages = []
    for entity in HOST_ENTITIES:
        payload = _config(entity, unique, state_topic, device, base)
        if entity.key == "reachable":
            payload["json_attributes_topic"] = state_topic
        messages.append((f"{config.discovery_prefix}/{entity.component}/{unique}/{entity.key}/config", payload))
    return messages


def supervisor_broker() -> MqttConfig | None:
    """Broker details from the Supervisor, when running as a Home Assistant app."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    request = urllib.request.Request(SUPERVISOR_MQTT_URL, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - fixed internal URL
            data = json.load(response).get("data", {})
    except Exception as exc:  # noqa: BLE001 - no broker configured is a normal case
        log.info("no MQTT service from the Supervisor: %s", exc)
        return None
    if not data.get("host"):
        return None
    return MqttConfig(
        host=data["host"],
        port=int(data.get("port", 1883)),
        username=data.get("username", "") or "",
        password=data.get("password", "") or "",
        tls=bool(data.get("ssl")),
    )


class MqttPublisher:
    """Publishes discovery and state; reconnects and re-announces by itself."""

    def __init__(self, settings: Settings, config: MqttConfig):
        self.settings = settings
        self.config = config
        self.client: mqtt.Client | None = None
        self._announced: set[str] = set()
        self._retired: set[str] = set()
        self._uniques: dict[str, str] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def _build(self) -> mqtt.Client:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"vmware-disk-health-{os.getpid()}")
        if self.config.username:
            client.username_pw_set(self.config.username, self.config.password)
        if self.config.tls:
            client.tls_set()
        client.will_set(f"{self.config.base_topic}/status", "offline", retain=True)
        client.on_connect = self._on_connect
        client.on_disconnect = lambda *_: log.warning("MQTT connection lost; reconnecting")
        return client

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties=None) -> None:
        if reason_code != 0:
            log.error("MQTT connection refused: %s", reason_code)
            return
        log.info("connected to MQTT broker %s:%s", self.config.host, self.config.port)
        client.publish(f"{self.config.base_topic}/status", "online", retain=True)
        # Home Assistant may have restarted: announce everything again.
        self._announced.clear()

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.client = self._build()
        self.client.connect_async(self.config.host, self.config.port, keepalive=60)
        self.client.loop_start()

    async def stop(self) -> None:
        if not self.client:
            return
        self.client.publish(f"{self.config.base_topic}/status", "offline", retain=True)
        await asyncio.sleep(0.2)
        self.client.loop_stop()
        self.client.disconnect()
        self.client = None

    def _unique_for(self, disk: DiskResult) -> str:
        """Serial-based, unless two disks claim the same serial."""
        unique = disk_unique(disk)
        owner = self._uniques.setdefault(unique, disk.key)
        if owner != disk.key:
            log.warning("two disks report serial %r; using the device id for %s", disk.info.serial, disk.key)
            return f"vdh_{slug(disk.key)}"
        return unique

    def _publish(self, topic: str, payload, retain: bool = True) -> None:
        if not self.client:
            return
        body = payload if isinstance(payload, str) else json.dumps(payload, default=str)
        self.client.publish(topic, body, qos=1, retain=retain)

    async def publish(self, host: HostResult, _changes=None) -> None:
        """Listener for the monitor: announce new devices, then publish states."""
        base = self.config.base_topic
        for topic, payload in host_messages(host, self.config):
            if topic not in self._announced:
                self._publish(topic, payload)
                self._announced.add(topic)
        self._publish(f"{base}/host/{slug(host.name)}/state", host_state(host, host.disks))
        for disk in host.disks:
            override = self.settings.override_for(disk.info)
            unique = self._unique_for(disk)
            if unique != legacy_disk_unique(disk) and disk.key not in self._retired:
                # Withdraw the entities of the pre-26.09.18 naming scheme, so
                # Home Assistant drops them instead of leaving duplicates behind.
                for topic in legacy_disk_topics(disk, self.config):
                    self._publish(topic, "")
                self._retired.add(disk.key)
            for topic, payload in disk_messages(disk, self.config, override.name if override else None, unique):
                if topic not in self._announced:
                    self._publish(topic, payload)
                    self._announced.add(topic)
            self._publish(f"{base}/disk/{slug(disk.key)}/state", disk_state(disk))
        self._publish(f"{base}/status", "online")
        self._publish(f"{base}/version", __version__)
