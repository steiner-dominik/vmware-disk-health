"""Parsers for the text output of esxcli on ESXi 8.

ESXi 8.0.3 has no JSON formatter, so these read the default human-readable
layouts: indented ``Key: Value`` blocks and fixed-column tables.
"""

from __future__ import annotations

import re

from ..model import DiskInfo, DiskKind, Reading

# NVMe critical warning flags as esxcli names them, in spec bit order.
NVME_WARNING_FLAGS = {
    "Available Spare Space Below Threshold": "spare_below_threshold",
    "Temperature Warning": "temperature",
    "NVM Subsystem Reliability Degradation": "reliability_degraded",
    "Read Only Mode": "read_only",
    "Volatile Memory Backup Device Failure": "volatile_backup_failed",
}

NVME_DATA_UNIT_BYTES = 512 * 1000


def parse_blocks(text: str) -> dict[str, dict[str, str]]:
    """Parse ``name`` headers followed by indented ``Key: Value`` lines."""
    blocks: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if not line[0].isspace():
            current = blocks.setdefault(line.strip().rstrip(":"), {})
            continue
        if current is None or ":" not in line:
            continue
        key, _, value = line.strip().partition(":")
        current[key.strip()] = value.strip()
    return blocks


def parse_key_values(text: str) -> dict[str, str]:
    """Flatten a single-block ``Key: Value`` output (header line optional)."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.strip().partition(":")
        if value.strip() or line[0].isspace():
            result[key.strip()] = value.strip()
    return result


def parse_table(text: str) -> list[dict[str, str]]:
    """Parse a fixed-width table whose second line is a row of dashes.

    Column boundaries come from the dash groups, so values containing single
    spaces (``Health Status``) stay intact.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines[:-1]):
        if re.fullmatch(r"[-\s]+", lines[index + 1]) and "-" in lines[index + 1]:
            header, rule, rows = line, lines[index + 1], lines[index + 2 :]
            break
    else:
        return []
    spans = [m.span() for m in re.finditer(r"-+", rule)]
    names = [header[start:end].strip() for start, end in _widen(spans, len(header))]
    result = []
    for row in rows:
        cells = [row[start:end].strip() for start, end in _widen(spans, len(row))]
        result.append(dict(zip(names, cells, strict=False)))
    return result


def _widen(spans: list[tuple[int, int]], length: int) -> list[tuple[int, int]]:
    # A column owns everything up to the start of the next one; the last one
    # runs to the end of the line.
    starts = [start for start, _ in spans]
    ends = starts[1:] + [max(length, spans[-1][1])]
    return list(zip(starts, ends, strict=True))


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


def parse_number(value: str | None) -> float | None:
    """Read esxcli numbers: decimal, ``0x`` hex, with or without a unit suffix."""
    if value is None:
        return None
    token = value.strip().split(" ")[0] if value.strip() else ""
    if not token or token.upper() in {"N/A", "NA", "UNKNOWN"}:
        return None
    try:
        return float(int(token, 16)) if token.lower().startswith("0x") else float(token)
    except ValueError:
        return None


def _int(value: str | None) -> int | None:
    number = parse_number(value)
    return None if number is None else int(number)


def parse_version(text: str) -> str | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[0] if lines else None


def t10_serial(device_id: str) -> str:
    """Serial number embedded in an ATA t10 identifier.

    ``t10.ATA_____<model, 40 chars>_<serial, 20 chars>`` with spaces encoded
    as underscores. NVMe t10 ids carry an EUI-64 instead, not a serial.
    """
    match = re.match(r"t10\.ATA_{5}(.+)$", device_id)
    if not match:
        return ""
    body = match.group(1)
    serial = body[-20:].strip("_") if len(body) >= 60 else ""
    return serial or body.rstrip("_").rsplit("_", 1)[-1]


def t10_model(device_id: str, listed_model: str) -> str:
    """Full model name from the t10 id, which esxcli truncates to 16 chars.

    The id encodes spaces as ``_`` and dashes as ``2D``; the decoded name is
    only trusted if it starts with the (truncated) model esxcli reports.
    """
    match = re.match(r"t10\.ATA_{5}(.+)$", device_id)
    listed = listed_model.strip()
    if not match or len(match.group(1)) < 60:
        return listed
    candidate = match.group(1)[:-20].rstrip("_").replace("_", " ").replace("2D", "-")
    return candidate if listed and candidate.startswith(listed) else listed


def parse_device_list(text: str) -> list[DiskInfo]:
    """Local physical disks from ``esxcli storage core device list``.

    Skips CD-ROMs, USB devices, pseudo/offline devices, RAID logical volumes
    and anything that is not local (SAN, iSCSI, vVols).
    """
    disks = []
    for device_id, props in parse_blocks(text).items():
        if props.get("Device Type", "").strip() != "Direct-Access":
            continue
        if not _bool(props.get("Is Local")) or _bool(props.get("Is USB")):
            continue
        if _bool(props.get("Is Pseudo")) or _bool(props.get("Is Offline")):
            continue
        if props.get("Drive Type", "").strip().lower() == "logical":
            continue
        if not props.get("Devfs Path", "").startswith("/vmfs/devices/disks/"):
            continue
        vendor = props.get("Vendor", "").strip()
        is_nvme = vendor.upper() == "NVME" or device_id.startswith("t10.NVMe")
        is_sas = _bool(props.get("Is SAS"))
        if is_nvme:
            kind = DiskKind.NVME
        elif _bool(props.get("Is SSD")):
            kind = DiskKind.SSD
        else:
            kind = DiskKind.HDD
        size_mb = _int(props.get("Size"))
        disks.append(
            DiskInfo(
                device_id=device_id,
                display_name=props.get("Display Name", ""),
                vendor=vendor,
                model=t10_model(device_id, props.get("Model", "")) if not is_nvme else props.get("Model", "").strip(),
                firmware=props.get("Revision", "").strip(),
                serial=t10_serial(device_id),
                size_bytes=size_mb * 1024 * 1024 if size_mb else None,
                kind=kind,
                protocol="nvme" if is_nvme else "sas" if is_sas else "ata",
                is_boot=_bool(props.get("Is Boot Device")),
            )
        )
    return disks


def parse_path_list(text: str) -> dict[str, str]:
    """Map device id -> adapter (vmhbaN) from ``esxcli storage core path list``."""
    mapping: dict[str, str] = {}
    for props in parse_blocks(text).values():
        device, adapter = props.get("Device"), props.get("Adapter")
        if device and adapter:
            mapping.setdefault(device, adapter)
    return mapping


def parse_nvme_adapters(text: str) -> list[str]:
    """Adapter names from ``esxcli nvme device list``."""
    return [row["HBA Name"] for row in parse_table(text) if row.get("HBA Name")]


def parse_nvme_device_get(text: str) -> dict[str, str]:
    """Identify-controller data from ``esxcli nvme device get -A vmhbaN``."""
    return parse_key_values(text)


def kelvin_to_celsius(value: str | None) -> float | None:
    number = parse_number(value)
    if number is None or number <= 0:
        return None
    return round(number - 273.15, 1) if (value or "").strip().upper().endswith("K") else number


def parse_nvme_smart_log(text: str) -> tuple[Reading, dict[str, str]]:
    """``esxcli nvme device log smart get -A vmhbaN`` -> reading + raw values."""
    props = parse_key_values(text)
    warnings = [flag for label, flag in NVME_WARNING_FLAGS.items() if _bool(props.get(label))]
    units_written = _int(props.get("Data Units Written"))
    units_read = _int(props.get("Data Units Read"))
    percentage_used = parse_number(props.get("Percentage Used"))
    reading = Reading(
        # NVMe has no pass/fail verdict; any critical warning bit is a failure.
        health_passed=not warnings if props else None,
        temperature_c=kelvin_to_celsius(props.get("Composite Temperature")),
        power_on_hours=_int(props.get("Power On Hours")),
        power_cycles=_int(props.get("Power Cycles")),
        unsafe_shutdowns=_int(props.get("Unsafe Shutdowns")),
        # Percentage Used may legitimately exceed 100 on worn drives.
        life_used_pct=percentage_used,
        written_bytes=units_written * NVME_DATA_UNIT_BYTES if units_written is not None else None,
        read_bytes=units_read * NVME_DATA_UNIT_BYTES if units_read is not None else None,
        media_errors=_int(props.get("Media Errors")),
        error_log_entries=_int(props.get("Number of Error Info Log Entries")),
        available_spare_pct=parse_number(props.get("Available Spare")),
        available_spare_threshold_pct=parse_number(props.get("Available Spare Threshold")),
        critical_warnings=warnings,
    )
    return reading, props


# Native SMART parameters that are unambiguous across drives. The remaining
# ones (reallocated sectors, sector counts, power-on hours) are reported as
# normalized values on some drives and raw values on others, so they are kept
# as raw data only until verified against real output.
_HEALTH_OK = {"OK"}


def parse_native_smart(text: str) -> tuple[Reading, dict[str, dict[str, str]]]:
    """``esxcli storage core device smart get -d <device>`` -> reading + raw table."""
    rows = {row.get("Parameter", ""): row for row in parse_table(text) if row.get("Parameter")}
    health = rows.get("Health Status", {}).get("Value")
    wearout = parse_number(rows.get("Media Wearout Indicator", {}).get("Value"))
    reading = Reading(
        health_passed=None if not health or health.upper() == "N/A" else health.upper() in _HEALTH_OK,
        temperature_c=parse_number(rows.get("Drive Temperature", {}).get("Value")),
        temperature_limit_c=parse_number(rows.get("Driver Rated Max Temperature", {}).get("Value")),
        # Media Wearout Indicator is a normalized 100 (new) .. 0 (worn out) value.
        life_used_pct=None if wearout is None else max(0.0, 100.0 - wearout),
    )
    return reading, rows
