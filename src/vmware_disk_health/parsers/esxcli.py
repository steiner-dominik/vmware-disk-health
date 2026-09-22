"""Parsers for esxcli output.

esxcli prints JSON only with the undocumented ``--debug --formatter=json``
flags, so every command is parsed from either JSON or the default text layout
(indented ``Key: Value`` blocks and fixed-column tables). Both are turned into
*records*: dicts keyed by the label with spaces and punctuation removed, which
is exactly how the JSON formatter names its keys (``Number of Error Info Log
Entries`` -> ``NumberofErrorInfoLogEntries``). The parsers below only ever
see records, so they work the same for both formats.
"""

from __future__ import annotations

import json
import re
from enum import Enum
from typing import Any

from ..model import DiskInfo, DiskKind, Reading

Record = dict[str, Any]

NVME_DATA_UNIT_BYTES = 512 * 1000

# NVMe critical warning bits, keyed by their record name.
NVME_WARNING_FLAGS = {
    "availablesparespacebelowthreshold": "spare_below_threshold",
    "temperaturewarning": "temperature",
    "nvmsubsystemreliabilitydegradation": "reliability_degraded",
    "readonlymode": "read_only",
    "volatilememorybackupdevicefailure": "volatile_backup_failed",
}


class Shape(Enum):
    BLOCKS = "blocks"  # list of named objects: device list, path list
    RECORD = "record"  # one object: version, nvme device get, nvme smart log
    TABLE = "table"  # list of rows: device smart get, nvme device list


def norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _record(mapping: dict[str, Any], *, keep_labels: bool = False) -> Record:
    record = {norm_key(k): v for k, v in mapping.items()}
    if keep_labels and mapping:  # original labels, for showing raw data to people
        record["_labels"] = {norm_key(k): k for k in mapping}
    return record


# ---------------------------------------------------------------- front-ends


def decode_json(text: str, shape: Shape) -> Record | list[Record]:
    """Raises ValueError if the output is not the JSON we expect."""
    data = json.loads(text)
    if isinstance(data, dict):
        data = [data] if shape is not Shape.RECORD else data
    if shape is Shape.RECORD:
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        if not isinstance(data, dict):
            raise ValueError(f"expected a JSON object, got {type(data).__name__}")
        return _record(data, keep_labels=True)
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ValueError("expected a JSON list of objects")
    return [_record(item) for item in data]


def decode_text(text: str, shape: Shape) -> Record | list[Record]:
    if shape is Shape.BLOCKS:
        return text_blocks(text)
    if shape is Shape.TABLE:
        return text_table(text)
    return text_record(text)


def text_blocks(text: str) -> list[Record]:
    """Unindented names followed by indented ``Key: Value`` lines.

    The name is kept as ``_name``; for the device list it is the device id.
    """
    blocks: list[Record] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if not line[0].isspace():
            blocks.append({"_name": line.strip().rstrip(":")})
        elif blocks and ":" in line:
            key, _, value = line.strip().partition(":")
            blocks[-1][norm_key(key)] = value.strip()
    return blocks


def text_record(text: str) -> Record:
    """A single ``Key: Value`` block; an unindented header line is ignored."""
    pairs: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line or not line[:1].isspace():
            continue
        key, _, value = line.strip().partition(":")
        pairs[key.strip()] = value.strip()
    return _record(pairs, keep_labels=True)


def text_table(text: str) -> list[Record]:
    """A fixed-width table whose second line is a row of dashes.

    Column boundaries come from the dash groups, so values containing single
    spaces (``Health Status``) stay intact.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines[:-1]):
        rule = lines[index + 1]
        if "-" in rule and re.fullmatch(r"[-\s]+", rule):
            header, rows = line, lines[index + 2 :]
            break
    else:
        return []
    starts = [m.start() for m in re.finditer(r"-+", rule)]

    def cells(row: str) -> list[str]:
        ends = starts[1:] + [max(len(row), len(rule))]
        return [row[start:end].strip() for start, end in zip(starts, ends, strict=True)]

    names = [norm_key(name) for name in cells(header)]
    return [dict(zip(names, cells(row), strict=True)) for row in rows]


# ------------------------------------------------------------------- values


def has_values(record: Record) -> bool:
    return any(key != "_labels" for key in record)


def as_str(value: Any) -> str:
    return "" if value is None else str(value).strip()


def as_bool(value: Any) -> bool:
    return value if isinstance(value, bool) else as_str(value).lower() == "true"


def as_number(value: Any) -> float | None:
    """Decimal, ``0x`` hex, with or without a unit suffix; None for N/A."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    text = as_str(value)
    token = text.split(" ")[0] if text else ""
    if not token or token.upper() in {"N/A", "NA", "UNKNOWN"}:
        return None
    try:
        return float(int(token, 16)) if token.lower().startswith("0x") else float(token)
    except ValueError:
        return None


def as_int(value: Any) -> int | None:
    number = as_number(value)
    return None if number is None else int(number)


def kelvin(value: Any) -> float | None:
    """NVMe reports temperatures in Kelvin, with or without a ``K`` suffix."""
    number = as_number(value)
    return round(number - 273.15, 1) if number and number > 0 else None


# ------------------------------------------------------------------ parsers


def parse_version(record: Record) -> str | None:
    """``esxcli system version get``."""
    version = as_str(record.get("version"))
    if not version:
        return None
    update = as_str(record.get("update"))
    build = re.sub(r"^\D*", "", as_str(record.get("build")))
    text = f"{as_str(record.get('product')) or 'VMware ESXi'} {version}"
    if update and update != "0":
        text += f" Update {update}"
    return f"{text} build-{build}" if build else text


def parse_vmware_vl(text: str) -> str | None:
    """``vmware -vl``, the fallback when esxcli has no JSON formatter."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[0] if lines else None


def t10_serial(device_id: str) -> str:
    """Serial number embedded in an ATA t10 identifier.

    ``t10.ATA_____<model, 40 chars><serial, 20 chars>`` with spaces encoded
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


def parse_device_list(records: list[Record]) -> list[DiskInfo]:
    """Local physical disks from ``esxcli storage core device list``.

    Skips CD-ROMs, USB devices, pseudo/offline devices, RAID logical volumes
    and anything that is not local (SAN, iSCSI, vVols).
    """
    disks = []
    for rec in records:
        device_id = as_str(rec.get("device")) or as_str(rec.get("_name"))
        if not device_id or as_str(rec.get("devicetype")) != "Direct-Access":
            continue
        if not as_bool(rec.get("islocal")) or as_bool(rec.get("isusb")):
            continue
        if as_bool(rec.get("ispseudo")) or as_bool(rec.get("isoffline")):
            continue
        if as_str(rec.get("drivetype")).lower() == "logical":
            continue
        if not as_str(rec.get("devfspath")).startswith("/vmfs/devices/disks/"):
            continue
        vendor = as_str(rec.get("vendor"))
        model = as_str(rec.get("model"))
        is_nvme = vendor.upper() == "NVME" or device_id.startswith("t10.NVMe")
        if is_nvme:
            kind = DiskKind.NVME
        elif as_bool(rec.get("isssd")):
            kind = DiskKind.SSD
        else:
            kind = DiskKind.HDD
        size_mb = as_int(rec.get("size"))
        disks.append(
            DiskInfo(
                device_id=device_id,
                display_name=as_str(rec.get("displayname")),
                vendor=vendor,
                model=model if is_nvme else t10_model(device_id, model),
                firmware=as_str(rec.get("revision")),
                serial=t10_serial(device_id),
                size_bytes=size_mb * 1024 * 1024 if size_mb else None,
                kind=kind,
                protocol="nvme" if is_nvme else "sas" if as_bool(rec.get("issas")) else "ata",
                is_boot=as_bool(rec.get("isbootdevice")),
            )
        )
    return disks


def parse_path_list(records: list[Record]) -> dict[str, str]:
    """Map device id -> adapter (vmhbaN) from ``esxcli storage core path list``."""
    mapping: dict[str, str] = {}
    for rec in records:
        device, adapter = as_str(rec.get("device")), as_str(rec.get("adapter"))
        if device and adapter:
            mapping.setdefault(device, adapter)
    return mapping


def parse_nvme_adapters(records: list[Record]) -> list[str]:
    """Adapter names from ``esxcli nvme device list``."""
    return [as_str(rec.get("hbaname")) for rec in records if as_str(rec.get("hbaname"))]


def parse_capacity_list(records: list[Record]) -> dict[str, tuple[int | None, str]]:
    """Device id -> (logical block size, format type) from ``esxcli storage core device capacity list``."""
    return {
        as_str(rec.get("device")): (as_int(rec.get("logicalblocksize")), as_str(rec.get("formattype")))
        for rec in records
        if as_str(rec.get("device"))
    }


def parse_nvme_device_get(record: Record) -> tuple[str, float | None]:
    """Serial number and temperature limit from ``esxcli nvme device get -A vmhbaN``.

    The limit is the critical composite threshold, or the warning threshold
    (where the drive starts throttling) when no critical one is set.
    """
    limit = kelvin(record.get("criticalcompositetemperaturethreshold")) or kelvin(
        record.get("warningcompositetemperaturethreshold")
    )
    return as_str(record.get("serialnumber")), limit


def parse_nvme_smart_log(record: Record) -> Reading:
    """``esxcli nvme device log smart get -A vmhbaN``."""
    warnings = [flag for key, flag in NVME_WARNING_FLAGS.items() if as_bool(record.get(key))]
    units_written = as_int(record.get("dataunitswritten"))
    units_read = as_int(record.get("dataunitsread"))
    return Reading(
        # NVMe has no pass/fail verdict; any critical warning bit is a failure.
        health_passed=not warnings if has_values(record) else None,
        temperature_c=kelvin(record.get("compositetemperature")),
        power_on_hours=as_int(record.get("poweronhours")),
        power_cycles=as_int(record.get("powercycles")),
        unsafe_shutdowns=as_int(record.get("unsafeshutdowns")),
        # Percentage Used may legitimately exceed 100 on worn drives.
        life_used_pct=as_number(record.get("percentageused")),
        written_bytes=units_written * NVME_DATA_UNIT_BYTES if units_written is not None else None,
        read_bytes=units_read * NVME_DATA_UNIT_BYTES if units_read is not None else None,
        media_errors=as_int(record.get("mediaerrors")),
        error_log_entries=as_int(record.get("numberoferrorinfologentries")),
        available_spare_pct=as_number(record.get("availablespare")),
        available_spare_threshold_pct=as_number(record.get("availablesparethreshold")),
        critical_warnings=warnings,
    )


HOST_WRITES_32MIB_BYTES = 32 * 1024 * 1024


def _writes_in_32mib_units(model: str) -> bool:
    """Whether Write/Read Sectors TOT Count (ATA attribute 241/242) counts
    32 MiB units instead of sectors, as Intel/Solidigm SATA data-center SSDs
    (model codes starting ``SSDSC``, e.g. the D3-S4610 ``SSDSC2BB016T7R``) do.

    Confirmed against a live host: on those drives the raw value is identical
    to Media Wearout Indicator's, which shares the same NAND-write counter --
    a documented Intel/Solidigm firmware convention. esxcli's attribute table
    is a fixed id->name mapping with no vendor awareness, and every other
    vendor in it (Samsung, Crucial/Micron, Seagate, ...) counts real sectors.
    """
    return "SSDSC" in model.upper()


def parse_native_smart(rows: list[Record], kind: DiskKind, logical_block_size: int | None = None, model: str = "") -> Reading:
    """``esxcli storage core device smart get -d <device>``.

    For ATA drives ``Value``/``Worst``/``Threshold`` are the *normalized*
    SMART values (a Samsung reports ``Drive Temperature 63`` at 37 °C), so
    real numbers come from the ``Raw`` column, which ESXi 8 prints. NVMe rows
    have no worst/raw and their ``Value`` is the real number.
    """
    by_name = {norm_key(as_str(row.get("parameter"))): row for row in rows}

    def normalized(row: Record) -> bool:
        return as_number(row.get("worst")) is not None

    def counter(name: str) -> int | None:
        row = by_name.get(name)
        if row is None:
            return None
        raw = as_int(row.get("raw"))
        if raw is not None:
            return raw
        # Without a raw value, only a non-normalized value is a real count.
        return None if normalized(row) else as_int(row.get("value"))

    temperature = None
    if row := by_name.get("drivetemperature"):
        raw = as_int(row.get("raw"))
        if raw is not None:
            temperature = float(raw & 0xFF)  # vendors pack min/max into the upper bytes
        elif not normalized(row):
            temperature = as_number(row.get("value"))

    life_used = None
    if kind is not DiskKind.HDD and (row := by_name.get("mediawearoutindicator")):
        wearout = as_number(row.get("value"))  # normalized: 100 new .. 0 worn out
        life_used = None if wearout is None else max(0.0, 100.0 - wearout)

    health = as_str(by_name.get("healthstatus", {}).get("value")).upper()
    written = counter("writesectorstotcount")
    read = counter("readsectorstotcount")
    if _writes_in_32mib_units(model):
        unit = HOST_WRITES_32MIB_BYTES
    else:
        unit = logical_block_size or 512

    failing = []
    for row in rows:
        value, threshold = as_number(row.get("value")), as_number(row.get("threshold"))
        if normalized(row) and value is not None and threshold and value <= threshold:
            failing.append(as_str(row.get("parameter")))

    return Reading(
        health_passed=None if health in {"", "N/A", "UNKNOWN"} else health == "OK",
        temperature_c=temperature,
        power_on_hours=counter("poweronhours"),
        power_cycles=counter("powercyclecount"),
        life_used_pct=life_used,
        written_bytes=written * unit if written is not None else None,
        read_bytes=read * unit if read is not None else None,
        # NVMe rows report a synthesized 0 here; their spare/media data comes from the NVMe log.
        reallocated_sectors=counter("reallocatedsectorcount") if kind is not DiskKind.NVME else None,
        pending_sectors=counter("pendingsectorreallocationcount"),
        offline_uncorrectable=counter("uncorrectablesectorcount"),
        reported_uncorrectable=counter("uncorrectableerrorcount"),
        failing_attributes=failing,
    )
