"""Normalized data model shared by collector, storage, API and integrations."""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Any

from pydantic import BaseModel, Field


class DiskKind(StrEnum):
    HDD = "hdd"
    SSD = "ssd"
    NVME = "nvme"


class Severity(IntEnum):
    """Ordered so that max() yields the overall status of a disk."""

    OK = 0
    UNKNOWN = 1
    WARNING = 2
    CRITICAL = 3

    @property
    def label(self) -> str:
        return self.name.lower()


class Finding(BaseModel):
    code: str
    severity: Severity
    message: str
    value: float | int | str | None = None
    limit: float | None = None


class DiskInfo(BaseModel):
    """What ESXi itself tells us about a device, before any SMART query."""

    device_id: str
    display_name: str = ""
    vendor: str = ""
    model: str = ""
    firmware: str = ""
    serial: str = ""
    size_bytes: int | None = None
    kind: DiskKind = DiskKind.HDD
    protocol: str = "ata"  # ata | nvme | sas
    is_boot: bool = False
    logical_block_size: int | None = None
    format_type: str = ""  # 512n, 512e, 4Kn as reported by ESXi
    nvme_adapter: str | None = None
    # vSAN OSA role from `esxcli vsan storage list`: "cache" or "capacity";
    # None when the disk is not claimed by vSAN (or the host has no vSAN).
    vsan_tier: str | None = None
    vsan_disk_group: str | None = None  # device id of the group's cache disk


class Reading(BaseModel):
    """Health values of one disk at one point in time.

    Every field is optional: which ones are filled depends on the drive and on
    which sources were available (native esxcli, NVMe log, smartctl).
    """

    health_passed: bool | None = None
    temperature_c: float | None = None
    temperature_limit_c: float | None = None
    power_on_hours: int | None = None
    power_cycles: int | None = None
    unsafe_shutdowns: int | None = None
    life_used_pct: float | None = None
    written_bytes: int | None = None
    read_bytes: int | None = None
    reallocated_sectors: int | None = None
    pending_sectors: int | None = None
    offline_uncorrectable: int | None = None
    reported_uncorrectable: int | None = None
    crc_errors: int | None = None
    media_errors: int | None = None
    error_log_entries: int | None = None
    available_spare_pct: float | None = None
    available_spare_threshold_pct: float | None = None
    critical_warnings: list[str] = Field(default_factory=list)
    # ATA attributes whose normalized value is at or below the vendor threshold.
    failing_attributes: list[str] = Field(default_factory=list)
    # Data written against the vendor's rated endurance (TBW). Only filled when
    # the drive reports no wear of its own, and never authoritative: see endurance.py.
    life_used_estimated_pct: float | None = None

    @property
    def life_remaining_pct(self) -> float | None:
        return None if self.life_used_pct is None else max(0.0, 100.0 - self.life_used_pct)

    @property
    def life_remaining_estimated_pct(self) -> float | None:
        if self.life_used_estimated_pct is None:
            return None
        return max(0.0, 100.0 - self.life_used_estimated_pct)

    def merge(self, other: Reading) -> Reading:
        """Fill fields that are still empty with values from a lower-priority source."""
        data = self.model_dump()
        for key, value in other.model_dump().items():
            if data.get(key) in (None, []) and value not in (None, []):
                data[key] = value
        return Reading(**data)


# Numeric reading fields that are stored as history and exposed as metrics.
NUMERIC_FIELDS: tuple[str, ...] = (
    "temperature_c",
    "power_on_hours",
    "power_cycles",
    "unsafe_shutdowns",
    "life_used_pct",
    "written_bytes",
    "read_bytes",
    "reallocated_sectors",
    "pending_sectors",
    "offline_uncorrectable",
    "reported_uncorrectable",
    "crc_errors",
    "media_errors",
    "error_log_entries",
    "available_spare_pct",
    "life_used_estimated_pct",
)


class EnduranceRating(BaseModel):
    """The vendor-published write endurance a disk was matched to."""

    family: str
    tbw_bytes: int
    # True when the model name was truncated (esxcli keeps 16 characters) and
    # several ratings could apply; the lowest one is used then.
    ambiguous: bool = False


def disk_key(host: str, device_id: str) -> str:
    """See DiskResult.key."""
    return f"{host}/{device_id}" if device_id.startswith("mpx.") else device_id


class DiskResult(BaseModel):
    """One disk after a collection run: identity, values, evaluation."""

    host: str
    info: DiskInfo
    reading: Reading = Field(default_factory=Reading)
    sources: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)
    status: Severity = Severity.UNKNOWN
    endurance: EnduranceRating | None = None

    @property
    def key(self) -> str:
        """Stable identity, independent of which data sources answered.

        t10/naa/eui ids are globally unique and follow a disk to another host;
        mpx ids are per-host bus paths, so those are qualified with the host.
        """
        return disk_key(self.host, self.info.device_id)


class HostResult(BaseModel):
    name: str
    address: str
    ok: bool = False
    error: str | None = None
    esxi_version: str | None = None
    smartctl_path: str | None = None
    esxcli_json: bool = False
    collected_at: float = 0.0
    duration_s: float = 0.0
    disks: list[DiskResult] = Field(default_factory=list)
    # Keys of disks the host reported but the configuration excludes, so they
    # are not mistaken for disks that disappeared.
    excluded: list[str] = Field(default_factory=list)
