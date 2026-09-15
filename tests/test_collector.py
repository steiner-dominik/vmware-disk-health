import asyncio

from conftest import fixture_text

from vmware_disk_health.collector import collect_host
from vmware_disk_health.config import HostConfig, Settings
from vmware_disk_health.evaluate import evaluate
from vmware_disk_health.model import DiskInfo, DiskKind, DiskResult, Reading, Severity
from vmware_disk_health.transport import CommandResult, FixtureTransport

EXOS = "t10.ATA_____ST20000NM007D2D3DJ103________________________________ZVTBWXYS"
SAMSUNG = "t10.ATA_____Samsung_SSD_750_EVO_120GB_______________S3F2NWBHB56997P_____"
OPTANE = "t10.NVMe____EO000750KWTXC___________________________00014935E3E4D25C"
SMARTCTL = "/opt/smartmontools/smartctl"


def responses(with_smartctl: bool) -> dict:
    native = fixture_text("synthetic/esxcli_storage_core_device_smart_get_ata.txt")
    result = {
        "vmware -vl": fixture_text("sa-esxi-01/vmware_-vl.txt"),
        "esxcli storage core device list": fixture_text("sa-esxi-01/esxcli_storage_core_device_list.txt"),
        "esxcli storage core path list": fixture_text("synthetic/esxcli_storage_core_path_list.txt"),
        "esxcli nvme device list": fixture_text("sa-esxi-01/esxcli_nvme_device_list.txt"),
        "esxcli nvme device log smart get -A vmhba4": fixture_text("sa-esxi-01/esxcli_nvme_device_log_smart_get_-A_vmhba4.txt"),
        f"esxcli storage core device smart get -d {EXOS}": native,
    }
    if with_smartctl:
        result[f"test -x {SMARTCTL}"] = ""
        result[f"{SMARTCTL} -j -x -n standby -d sat,auto /dev/disks/{EXOS}"] = fixture_text(
            "sa-esxi-01/smartctl_seagate_exos_x20.json"
        )
        result[f"{SMARTCTL} -j -x -n standby -d sat,auto /dev/disks/{SAMSUNG}"] = CommandResult(
            fixture_text("sa-esxi-01/smartctl_samsung_750_evo.json"), exit_status=4
        )
    return result


def run_collection(with_smartctl: bool, settings: Settings | None = None):
    host = HostConfig(name="sa-esxi-01", address="10.0.0.1")
    settings = settings or Settings(hosts=[host], data_dir="/tmp/unused")
    transport = FixtureTransport(responses(with_smartctl))

    async def connect(_host):
        return transport

    result = asyncio.run(collect_host(host, settings, connect, lambda key: None))
    return result, {d.info.device_id: d for d in result.disks}, transport


def test_collection_without_smartctl_uses_native_tools_only():
    result, disks, transport = run_collection(with_smartctl=False)
    assert result.ok and result.smartctl_path is None
    assert result.esxi_version == "VMware ESXi 8.0.3 build-25205845"
    assert len(disks) == 7
    assert not any("smartctl -j" in c for c in transport.commands)

    assert disks[EXOS].sources == ["esxcli-smart"]
    assert disks[EXOS].reading.temperature_c == 30
    assert disks[EXOS].status is Severity.OK

    optane = disks[OPTANE]
    assert optane.info.nvme_adapter == "vmhba4"
    assert optane.sources == ["esxcli-nvme"]
    assert optane.reading.written_bytes == 0x52C46FC1 * 512_000
    assert optane.status is Severity.OK

    # No capture for these: no data means unknown, never a false "ok".
    assert disks[SAMSUNG].status is Severity.UNKNOWN
    assert disks[SAMSUNG].errors


def test_collection_with_smartctl_prefers_its_data():
    _, disks, _ = run_collection(with_smartctl=True)
    exos = disks[EXOS]
    assert exos.sources == ["smartctl", "esxcli-smart"]
    assert exos.reading.written_bytes == 175524780300 * 512
    assert exos.reading.reallocated_sectors == 0
    assert exos.info.serial == "ZVTBWXYS"
    # Exit status 4 (a SMART command failed) still carries usable data.
    samsung = disks[SAMSUNG]
    assert samsung.sources == ["smartctl"]
    assert samsung.reading.life_remaining_pct == 77
    assert samsung.status is Severity.OK
    # smartctl cannot open NVMe on ESXi, so it is never tried for them.
    assert disks[OPTANE].sources == ["esxcli-nvme"]


def test_exclude_patterns():
    host = HostConfig(name="sa-esxi-01", address="10.0.0.1", exclude="*Samsung*, t10.NVMe*")
    settings = Settings(hosts=[host], data_dir="/tmp/unused")
    transport = FixtureTransport(responses(False))

    async def connect(_host):
        return transport

    result = asyncio.run(collect_host(host, settings, connect, lambda key: None))
    assert {d.info.kind for d in result.disks} == {DiskKind.HDD}


def test_unreachable_host_is_reported_not_raised():
    host = HostConfig(name="down", address="10.0.0.9")

    async def connect(_host):
        raise OSError("Connection refused")

    result = asyncio.run(collect_host(host, Settings(hosts=[host], data_dir="/tmp/unused"), connect, lambda key: None))
    assert not result.ok
    assert "refused" in result.error


def _disk(kind: DiskKind, **values) -> DiskResult:
    return DiskResult(host="h", info=DiskInfo(device_id="d", kind=kind), reading=Reading(**values), sources=["test"])


def test_evaluate_thresholds():
    settings = Settings()
    t = settings.thresholds
    assert evaluate(_disk(DiskKind.SSD, life_used_pct=85), t).status is Severity.WARNING
    assert evaluate(_disk(DiskKind.SSD, life_used_pct=95), t).status is Severity.CRITICAL
    assert evaluate(_disk(DiskKind.HDD, pending_sectors=1), t).status is Severity.CRITICAL
    assert evaluate(_disk(DiskKind.HDD, reallocated_sectors=4), t).status is Severity.WARNING
    rising = evaluate(_disk(DiskKind.HDD, reallocated_sectors=8), t, previous=Reading(reallocated_sectors=4))
    assert rising.status is Severity.CRITICAL
    assert evaluate(_disk(DiskKind.HDD, crc_errors=5), t, previous=Reading(crc_errors=5)).status is Severity.OK
    assert evaluate(_disk(DiskKind.HDD, crc_errors=6), t, previous=Reading(crc_errors=5)).status is Severity.WARNING
    assert evaluate(_disk(DiskKind.NVME, critical_warnings=["temperature"]), t).status is Severity.CRITICAL
    assert evaluate(_disk(DiskKind.HDD, health_passed=False), t).status is Severity.CRITICAL


def test_evaluate_temperature_respects_drive_limit():
    t = Settings().thresholds
    # SSD defaults are 60/70, but this drive is only rated for 55.
    disk = evaluate(_disk(DiskKind.SSD, temperature_c=52, temperature_limit_c=55), t)
    assert disk.status is Severity.WARNING
    assert evaluate(_disk(DiskKind.HDD, temperature_c=49, temperature_limit_c=60), t).status is Severity.OK


def test_disk_override_thresholds():
    settings = Settings(disk_overrides=[{"match": "S3F2*", "life_remaining_warn_pct": 80}])
    info = DiskInfo(device_id="x", serial="S3F2NWBHB56997P", kind=DiskKind.SSD)
    disk = DiskResult(host="h", info=info, reading=Reading(life_used_pct=23), sources=["test"])
    assert evaluate(disk, settings.thresholds_for(info)).status is Severity.WARNING
