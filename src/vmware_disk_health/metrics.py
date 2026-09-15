"""Prometheus text exposition of the latest state."""

from __future__ import annotations

from .model import DiskResult

# (metric suffix, reading field, help text, type)
DISK_METRICS = (
    ("temperature_celsius", "temperature_c", "Current drive temperature.", "gauge"),
    ("life_used_percent", "life_used_pct", "Rated endurance used (SSD/NVMe).", "gauge"),
    ("written_bytes_total", "written_bytes", "Bytes written over the drive's lifetime.", "counter"),
    ("read_bytes_total", "read_bytes", "Bytes read over the drive's lifetime.", "counter"),
    ("power_on_hours", "power_on_hours", "Power-on hours.", "counter"),
    ("power_cycles_total", "power_cycles", "Power cycles.", "counter"),
    ("unsafe_shutdowns_total", "unsafe_shutdowns", "Unsafe shutdowns (NVMe).", "counter"),
    ("reallocated_sectors", "reallocated_sectors", "Reallocated sectors.", "gauge"),
    ("pending_sectors", "pending_sectors", "Sectors pending reallocation.", "gauge"),
    ("offline_uncorrectable_sectors", "offline_uncorrectable", "Offline uncorrectable sectors.", "gauge"),
    ("reported_uncorrectable_errors_total", "reported_uncorrectable", "Reported uncorrectable errors.", "counter"),
    ("crc_errors_total", "crc_errors", "Interface CRC errors.", "counter"),
    ("media_errors_total", "media_errors", "Media and data integrity errors (NVMe).", "counter"),
    ("available_spare_percent", "available_spare_pct", "Available spare capacity.", "gauge"),
)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(**labels: str) -> str:
    return "{" + ",".join(f'{k}="{_escape(str(v))}"' for k, v in labels.items()) + "}"


def render(hosts: list[dict], disks: list[DiskResult]) -> str:
    lines: list[str] = []

    def family(name: str, help_text: str, kind: str) -> None:
        lines.append(f"# HELP vmware_disk_health_{name} {help_text}")
        lines.append(f"# TYPE vmware_disk_health_{name} {kind}")

    family("host_up", "1 if the last collection from the host succeeded.", "gauge")
    for host in hosts:
        up = int(bool(host.get("last_success")) and host.get("last_success") == host.get("last_attempt"))
        lines.append(f"vmware_disk_health_host_up{_labels(host=host['name'])} {up}")
    family("host_last_success_timestamp_seconds", "Time of the last successful collection.", "gauge")
    for host in hosts:
        if host.get("last_success"):
            lines.append(
                f"vmware_disk_health_host_last_success_timestamp_seconds{_labels(host=host['name'])} {host['last_success']}"
            )

    def disk_labels(disk: DiskResult) -> str:
        return _labels(
            host=disk.host, device=disk.info.device_id, model=disk.info.model, serial=disk.info.serial, kind=disk.info.kind.value
        )

    family("disk_status", "0 ok, 1 unknown, 2 warning, 3 critical.", "gauge")
    for disk in disks:
        lines.append(f"vmware_disk_health_disk_status{disk_labels(disk)} {int(disk.status)}")
    for suffix, field, help_text, kind in DISK_METRICS:
        values = [(disk, getattr(disk.reading, field)) for disk in disks]
        values = [(disk, value) for disk, value in values if value is not None]
        if not values:
            continue
        family(f"disk_{suffix}", help_text, kind)
        lines.extend(f"vmware_disk_health_disk_{suffix}{disk_labels(disk)} {value}" for disk, value in values)
    return "\n".join(lines) + "\n"
