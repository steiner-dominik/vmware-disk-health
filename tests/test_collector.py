import asyncio

import pytest
from conftest import fixture_text

from vmware_disk_health.collector import ESXCLI_JSON, collect_host
from vmware_disk_health.config import HostConfig, Settings
from vmware_disk_health.evaluate import evaluate
from vmware_disk_health.model import DiskInfo, DiskKind, DiskResult, Reading, Severity
from vmware_disk_health.transport import CommandResult, FixtureTransport

EXOS = "t10.ATA_____ST20000NM007D2D3DJ103________________________________ZVTBWXYS"
SAMSUNG = "t10.ATA_____Samsung_SSD_750_EVO_120GB_______________S3F2NWBHB56997P_____"
OPTANE = "t10.NVMe____EO000750KWTXC___________________________00014935E3E4D25C"
SN850X = "t10.NVMe____WD_BLACK_SN850X_4000GB__________________4695514E8B441B00"
SMARTCTL = "/opt/smartmontools/smartctl"
UNSUPPORTED_FORMATTER = CommandResult("Unable to find requested formatter: json\n", exit_status=1)


def sa_esxi_01(*, json: bool, smartctl: bool) -> dict:
    """Recorded responses of sa-esxi-01; commands without a capture fail."""
    f = fixture_text
    responses = {
        "vmware -vl": f("sa-esxi-01/vmware_-vl.txt"),
        "esxcli storage core device list": f("sa-esxi-01/esxcli_storage_core_device_list.txt"),
        "esxcli storage core device capacity list": f("sa-esxi-01/esxcli_storage_core_device_capacity_list.txt"),
        "esxcli storage core path list": f("sa-esxi-01/esxcli_storage_core_path_list.txt"),
        "esxcli nvme device get -A vmhba4": f("sa-esxi-01/esxcli_nvme_device_get_-A_vmhba4.txt"),
        "esxcli nvme device log smart get -A vmhba4": f("sa-esxi-01/esxcli_nvme_device_log_smart_get_-A_vmhba4.txt"),
        f"esxcli storage core device smart get -d {EXOS}": f("sa-esxi-01/esxcli_storage_core_device_smart_get_exos.txt"),
        f"esxcli storage core device smart get -d {SAMSUNG}": f("sa-esxi-01/esxcli_storage_core_device_smart_get_samsung.txt"),
        f"esxcli storage core device smart get -d {OPTANE}": f("sa-esxi-01/esxcli_storage_core_device_smart_get_optane.txt"),
        f"{ESXCLI_JSON} system version get": UNSUPPORTED_FORMATTER,
    }
    if json:
        responses[f"{ESXCLI_JSON} system version get"] = f("sa-esxi-01/esxcli_json_system_version_get.json")
        responses[f"{ESXCLI_JSON} storage core device list"] = f("sa-esxi-01/esxcli_json_storage_core_device_list.json")
        responses[f"{ESXCLI_JSON} storage core device capacity list"] = f(
            "sa-esxi-01/esxcli_json_storage_core_device_capacity_list.json"
        )
        responses[f"{ESXCLI_JSON} nvme device log smart get -A vmhba4"] = f(
            "sa-esxi-01/esxcli_json_nvme_device_log_smart_get_-A_vmhba4.json"
        )
    if smartctl:
        responses[f"test -x {SMARTCTL}"] = ""
        responses[f"{SMARTCTL} -j -x -n standby -d sat,auto /dev/disks/{EXOS}"] = f("sa-esxi-01/smartctl_seagate_exos_x20.json")
        # Exit status 4 (some SMART command failed) still carries usable data.
        responses[f"{SMARTCTL} -j -x -n standby -d sat,auto /dev/disks/{SAMSUNG}"] = CommandResult(
            f("sa-esxi-01/smartctl_samsung_750_evo.json"), exit_status=4
        )
    return responses


def collect(responses: dict, host: HostConfig | None = None, baseline=lambda key: (None, None)):
    host = host or HostConfig(name="sa-esxi-01", address="10.0.0.1")
    transport = FixtureTransport(responses)

    async def connect(_host):
        return transport

    result = asyncio.run(collect_host(host, Settings(hosts=[host], data_dir="/tmp/unused"), connect, baseline))
    return result, {d.info.device_id: d for d in result.disks}, transport


def test_without_smartctl_everything_comes_from_esxcli():
    result, disks, transport = collect(sa_esxi_01(json=False, smartctl=False))
    assert result.ok and result.smartctl_path is None and not result.esxcli_json
    assert result.esxi_version == "VMware ESXi 8.0.3 build-25205845"
    assert len(disks) == 7
    assert not any("smartctl -j" in c for c in transport.commands)

    exos = disks[EXOS]
    assert exos.sources == ["esxcli-smart"]
    assert (exos.reading.temperature_c, exos.reading.power_on_hours) == (30, 23065)
    assert exos.info.format_type == "512e"
    assert exos.status is Severity.OK

    samsung = disks[SAMSUNG]
    assert (samsung.reading.temperature_c, samsung.reading.life_remaining_pct) == (37, 77)
    assert samsung.status is Severity.OK

    optane = disks[OPTANE]
    assert optane.info.nvme_adapter == "vmhba4"
    assert optane.info.serial == "PHKE018000T8750BGN"
    assert optane.sources == ["esxcli-nvme", "esxcli-smart"]
    assert optane.reading.written_bytes == 0x52C46FC1 * 512_000
    assert optane.reading.temperature_limit_c == pytest.approx(69.9, abs=0.1)
    assert optane.status is Severity.OK

    # No capture for this one: no data means unknown, never a false "ok".
    assert disks[SN850X].status is Severity.UNKNOWN
    assert disks[SN850X].errors


def test_json_formatter_is_used_when_available_with_text_fallback():
    result, disks, transport = collect(sa_esxi_01(json=True, smartctl=False))
    assert result.esxcli_json
    assert result.esxi_version == "VMware ESXi 8.0.3 Update 3 build-25205845"
    assert f"{ESXCLI_JSON} nvme device log smart get -A vmhba4" in transport.commands
    assert "esxcli nvme device log smart get -A vmhba4" not in transport.commands
    assert "esxcli storage core device list" not in transport.commands
    # Commands without a JSON capture fell back to text and still produced data.
    assert "esxcli storage core path list" in transport.commands
    assert len(disks) == 7
    assert disks[OPTANE].reading.power_on_hours == 25542


def test_smartctl_data_takes_priority_when_installed():
    result, disks, transport = collect(sa_esxi_01(json=False, smartctl=True))
    assert result.smartctl_path == SMARTCTL
    exos = disks[EXOS]
    assert exos.sources == ["smartctl", "esxcli-smart"]
    assert exos.reading.written_bytes == 175524780300 * 512  # smartctl's value, not esxcli's later one
    assert exos.reading.crc_errors == 0  # only smartctl has this
    samsung = disks[SAMSUNG]
    assert samsung.sources == ["smartctl", "esxcli-smart"]
    assert samsung.reading.life_remaining_pct == 77
    # smartctl cannot open NVMe devices on ESXi, so it is never tried for them.
    assert not any("smartctl -j" in c and "NVMe" in c for c in transport.commands)


def test_exclude_patterns():
    host = HostConfig(name="sa-esxi-01", address="10.0.0.1", exclude="*Samsung*, t10.NVMe*")
    result, _, _ = collect(sa_esxi_01(json=False, smartctl=False), host)
    assert {d.info.kind for d in result.disks} == {DiskKind.HDD}


def test_missing_device_list_fails_the_host():
    responses = sa_esxi_01(json=False, smartctl=False)
    del responses["esxcli storage core device list"]
    result, _, _ = collect(responses)
    assert not result.ok and "device list" in result.error


def test_unreachable_host_is_reported_not_raised():
    host = HostConfig(name="down", address="10.0.0.9")

    async def connect(_host):
        raise OSError("Connection refused")

    result = asyncio.run(collect_host(host, Settings(hosts=[host], data_dir="/tmp/unused"), connect, lambda key: (None, None)))
    assert not result.ok
    assert "refused" in result.error


def _disk(kind: DiskKind, **values) -> DiskResult:
    return DiskResult(host="h", info=DiskInfo(device_id="d", kind=kind), reading=Reading(**values), sources=["test"])


def test_evaluate_thresholds():
    t = Settings().thresholds
    assert evaluate(_disk(DiskKind.SSD, life_used_pct=85), t).status is Severity.WARNING
    assert evaluate(_disk(DiskKind.SSD, life_used_pct=95), t).status is Severity.CRITICAL
    # A pending sector is a warning once, and critical when it is still there next poll.
    assert evaluate(_disk(DiskKind.HDD, pending_sectors=1), t).status is Severity.WARNING
    still = evaluate(_disk(DiskKind.HDD, pending_sectors=1), t, previous=Reading(pending_sectors=1))
    assert still.status is Severity.CRITICAL and still.findings[0].code == "pending_sectors_persisting"
    gone = evaluate(_disk(DiskKind.HDD, pending_sectors=1), t, previous=Reading(pending_sectors=0))
    assert gone.status is Severity.WARNING
    assert evaluate(_disk(DiskKind.HDD, reallocated_sectors=4), t).status is Severity.WARNING
    rising = evaluate(_disk(DiskKind.HDD, reallocated_sectors=8), t, baseline=Reading(reallocated_sectors=4))
    assert rising.status is Severity.CRITICAL
    assert evaluate(_disk(DiskKind.HDD, crc_errors=5), t, baseline=Reading(crc_errors=5)).status is Severity.OK
    assert evaluate(_disk(DiskKind.HDD, crc_errors=6), t, baseline=Reading(crc_errors=5)).status is Severity.WARNING
    assert evaluate(_disk(DiskKind.NVME, critical_warnings=["temperature"]), t).status is Severity.CRITICAL
    assert evaluate(_disk(DiskKind.HDD, health_passed=False), t).status is Severity.CRITICAL
    assert evaluate(_disk(DiskKind.HDD, failing_attributes=["Spin_Retry_Count"]), t).status is Severity.CRITICAL


def test_evaluate_temperature_respects_drive_limit():
    t = Settings().thresholds
    # SSD defaults are 60/70, but this drive is only rated for 55.
    assert evaluate(_disk(DiskKind.SSD, temperature_c=52, temperature_limit_c=55), t).status is Severity.WARNING
    assert evaluate(_disk(DiskKind.HDD, temperature_c=49, temperature_limit_c=60), t).status is Severity.OK


def test_disk_override_thresholds():
    settings = Settings(disk_overrides=[{"match": "S3F2*", "life_remaining_warn_pct": 80}])
    info = DiskInfo(device_id="x", serial="S3F2NWBHB56997P", kind=DiskKind.SSD)
    disk = DiskResult(host="h", info=info, reading=Reading(life_used_pct=23), sources=["test"])
    assert evaluate(disk, settings.thresholds_for(info)).status is Severity.WARNING
