"""Parser for ``smartctl -j -x`` JSON (smartmontools 7.x) of ATA drives."""

from __future__ import annotations

import json
import re
from typing import Any

from ..model import Reading

# Exit status bits 0 and 1: command line error, device could not be opened.
# The remaining bits describe drive state and still come with usable data.
_FATAL_EXIT_BITS = 0b11


class SmartctlError(Exception):
    pass


def _leading_int(raw: dict[str, Any] | None) -> int | None:
    """Raw attribute value as a plain counter.

    Vendors pack extra data into the 48-bit raw field (``30 (Min/Max 25/47)``),
    so the leading number of the decoded string is the reliable counter.
    """
    if not raw:
        return None
    match = re.match(r"\s*(\d+)", str(raw.get("string", "")))
    if match:
        return int(match.group(1))
    value = raw.get("value")
    return int(value) if isinstance(value, int) else None


def _device_stat(data: dict[str, Any], name: str) -> int | None:
    for page in data.get("ata_device_statistics", {}).get("pages", []):
        for entry in page.get("table", []):
            if entry.get("name") == name and entry.get("flags", {}).get("valid") and "value" in entry:
                return int(entry["value"])
    return None


def parse_smartctl(text: str) -> tuple[Reading, dict[str, Any]]:
    """Return the reading plus a compact raw view (attributes by id).

    Raises SmartctlError when smartctl could not read the device at all.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SmartctlError(f"invalid smartctl JSON: {exc}") from exc

    info = data.get("smartctl", {})
    messages = [m.get("string", "") for m in info.get("messages", [])]
    if any("STANDBY" in message.upper() for message in messages):
        raise SmartctlError("drive is in standby, skipped to avoid spinning it up")
    if int(info.get("exit_status", 0)) & _FATAL_EXIT_BITS:
        raise SmartctlError("; ".join(messages) or f"smartctl exit status {info.get('exit_status')}")

    attrs = {a["id"]: a for a in data.get("ata_smart_attributes", {}).get("table", [])}
    by_name = {a.get("name", ""): a for a in attrs.values()}
    block = int(data.get("logical_block_size") or 512)

    def raw(attr_id: int) -> int | None:
        return _leading_int(attrs.get(attr_id, {}).get("raw"))

    written_lbas = _device_stat(data, "Logical Sectors Written")
    if written_lbas is None:
        # 241 on most drives, 246 on Crucial/Micron (where 241 may be absent).
        written_lbas = _leading_int((by_name.get("Total_LBAs_Written") or {}).get("raw"))
    read_lbas = _device_stat(data, "Logical Sectors Read")
    if read_lbas is None:
        read_lbas = _leading_int((by_name.get("Total_LBAs_Read") or {}).get("raw"))

    temperature = data.get("temperature", {})
    spare = data.get("spare_available", {})
    smart_status = data.get("smart_status", {})

    reading = Reading(
        health_passed=smart_status.get("passed") if "passed" in smart_status else None,
        temperature_c=temperature.get("current") if temperature.get("current") is not None else raw(194),
        temperature_limit_c=temperature.get("limit_max") or temperature.get("op_limit_max"),
        power_on_hours=data.get("power_on_time", {}).get("hours", raw(9)),
        power_cycles=data.get("power_cycle_count", raw(12)),
        life_used_pct=_life_used(data, attrs),
        written_bytes=written_lbas * block if written_lbas is not None else None,
        read_bytes=read_lbas * block if read_lbas is not None else None,
        reallocated_sectors=raw(5) if 5 in attrs else _device_stat(data, "Number of Reallocated Logical Sectors"),
        pending_sectors=raw(197) if 197 in attrs else _device_stat(data, "Number of Realloc. Candidate Logical Sectors"),
        offline_uncorrectable=raw(198),
        reported_uncorrectable=raw(187) if 187 in attrs else _device_stat(data, "Number of Reported Uncorrectable Errors"),
        crc_errors=raw(199) if 199 in attrs else _device_stat(data, "Number of Interface CRC Errors"),
        error_log_entries=data.get("ata_smart_error_log", {}).get("extended", {}).get("count"),
        # Only meaningful for SSDs; HDDs report a static 100/10 here.
        available_spare_pct=spare.get("current_percent") if data.get("rotation_rate") == 0 else None,
        available_spare_threshold_pct=spare.get("threshold_percent") if data.get("rotation_rate") == 0 else None,
    )

    compact = {
        "model_family": data.get("model_family"),
        "model_name": data.get("model_name"),
        "serial_number": data.get("serial_number"),
        "firmware_version": data.get("firmware_version"),
        "rotation_rate": data.get("rotation_rate"),
        "capacity_bytes": data.get("user_capacity", {}).get("bytes"),
        "logical_block_size": block,
        "exit_status": info.get("exit_status"),
        "messages": messages,
        "attributes": [
            {
                "id": a["id"],
                "name": a.get("name"),
                "value": a.get("value"),
                "worst": a.get("worst"),
                "thresh": a.get("thresh"),
                "when_failed": a.get("when_failed"),
                "raw": a.get("raw", {}).get("string"),
                "prefailure": a.get("flags", {}).get("prefailure"),
            }
            for a in attrs.values()
        ],
    }
    return reading, compact


def _life_used(data: dict[str, Any], attrs: dict[int, dict[str, Any]]) -> float | None:
    """Percent of rated SSD endurance used, from the best available source."""
    if data.get("rotation_rate") not in (0, None):
        return None
    endurance = data.get("endurance_used", {}).get("current_percent")
    if endurance is not None:
        return float(endurance)
    stat = _device_stat(data, "Percentage Used Endurance Indicator")
    if stat is not None:
        return float(stat)
    by_name = {a.get("name", ""): a for a in attrs.values()}
    # Crucial/Micron: raw value of 202 is percent used.
    if "Percent_Lifetime_Remain" in by_name:
        used = _leading_int(by_name["Percent_Lifetime_Remain"].get("raw"))
        if used is not None:
            return float(used)
    # Normalized "remaining" style attributes: 100 new, 0 worn out.
    for name in ("Wear_Leveling_Count", "SSD_Life_Left", "Media_Wearout_Indicator", "Percent_Life_Remaining"):
        if name in by_name and by_name[name].get("value") is not None:
            return float(max(0, 100 - int(by_name[name]["value"])))
    return None
