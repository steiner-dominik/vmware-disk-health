"""Settings, loaded from Home Assistant's options.json or a standalone YAML file.

Both use the same structure, so one image serves both deployments.
"""

from __future__ import annotations

import json
import os
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from .model import DiskInfo, DiskKind

HA_OPTIONS_PATH = Path("/data/options.json")


def _split_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item).strip() for item in value if str(item).strip()]


class HostConfig(BaseModel):
    name: str
    address: str
    port: int = 22
    username: str = "root"
    password: str | None = None
    # "auto" probes the usual community VIB location, "none" never uses smartctl.
    smartctl_path: str = "auto"
    # Glob patterns matched against device id, serial and model.
    exclude: list[str] = Field(default_factory=list)
    enabled: bool = True

    @field_validator("exclude", mode="before")
    @classmethod
    def _exclude(cls, value: Any) -> list[str]:
        return _split_list(value)

    @field_validator("password", mode="before")
    @classmethod
    def _password(cls, value: Any) -> str | None:
        return value or None


class Thresholds(BaseModel):
    life_remaining_warn_pct: float = 20
    life_remaining_crit_pct: float = 10
    temp_hdd_warn_c: float = 50
    temp_hdd_crit_c: float = 60
    temp_ssd_warn_c: float = 60
    temp_ssd_crit_c: float = 70
    temp_nvme_warn_c: float = 70
    temp_nvme_crit_c: float = 80
    # Lower the limits to what the drive itself reports as its maximum.
    use_drive_temp_limit: bool = True

    def temperature_limits(self, kind: DiskKind) -> tuple[float, float]:
        return (
            getattr(self, f"temp_{kind.value}_warn_c"),
            getattr(self, f"temp_{kind.value}_crit_c"),
        )


class DiskOverride(BaseModel):
    """Per-disk adjustments, selected by a glob on device id, serial or model."""

    match: str
    ignore: bool = False
    name: str | None = None
    life_remaining_warn_pct: float | None = None
    life_remaining_crit_pct: float | None = None
    temp_warn_c: float | None = None
    temp_crit_c: float | None = None

    def matches(self, info: DiskInfo) -> bool:
        return any(fnmatch(candidate, self.match) for candidate in (info.device_id, info.serial, info.model) if candidate)


def default_data_dir() -> Path:
    if env := os.environ.get("VDH_DATA_DIR"):
        return Path(env)
    return Path("/data") if Path("/data").is_dir() else Path("data")


class Settings(BaseModel):
    hosts: list[HostConfig] = Field(default_factory=list)
    poll_interval_minutes: int = Field(default=60, ge=5, le=1440)
    retention_raw_days: int = Field(default=90, ge=1)
    command_timeout_seconds: int = Field(default=90, ge=10)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    disk_overrides: list[DiskOverride] = Field(default_factory=list)
    data_dir: Path = Field(default_factory=default_data_dir)
    log_level: str = "info"

    def override_for(self, info: DiskInfo) -> DiskOverride | None:
        return next((o for o in self.disk_overrides if o.matches(info)), None)

    def is_excluded(self, host: HostConfig, info: DiskInfo) -> bool:
        candidates = [c for c in (info.device_id, info.serial, info.model) if c]
        if any(fnmatch(c, pattern) for pattern in host.exclude for c in candidates):
            return True
        override = self.override_for(info)
        return bool(override and override.ignore)

    def thresholds_for(self, info: DiskInfo) -> Thresholds:
        override = self.override_for(info)
        if not override:
            return self.thresholds
        data = self.thresholds.model_dump()
        for field in ("life_remaining_warn_pct", "life_remaining_crit_pct"):
            if getattr(override, field) is not None:
                data[field] = getattr(override, field)
        if override.temp_warn_c is not None:
            data[f"temp_{info.kind.value}_warn_c"] = override.temp_warn_c
        if override.temp_crit_c is not None:
            data[f"temp_{info.kind.value}_crit_c"] = override.temp_crit_c
        return Thresholds(**data)


def load_settings(path: Path | None = None) -> Settings:
    """Explicit path or $VDH_CONFIG, else HA options.json, else ./config.yaml."""
    candidates = [path] if path else []
    if env := os.environ.get("VDH_CONFIG"):
        candidates.append(Path(env))
    candidates += [HA_OPTIONS_PATH, Path("/data/config.yaml"), Path("config.yaml")]
    for candidate in candidates:
        if candidate and candidate.is_file():
            text = candidate.read_text()
            raw = json.loads(text) if candidate.suffix == ".json" else yaml.safe_load(text) or {}
            return Settings(**raw)
    if path:
        raise FileNotFoundError(path)
    raise FileNotFoundError("no configuration found (config.yaml, /data/options.json or $VDH_CONFIG)")
