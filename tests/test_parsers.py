import pytest
from conftest import fixture_text

from vmware_disk_health.model import DiskKind
from vmware_disk_health.parsers import esxcli
from vmware_disk_health.parsers.smartctl import SmartctlError, parse_smartctl

REAL = "sa-esxi-01/"


def test_device_list_keeps_local_physical_disks_only():
    disks = esxcli.parse_device_list(fixture_text(REAL + "esxcli_storage_core_device_list.txt"))
    ids = [d.device_id for d in disks]
    assert len(disks) == 7
    assert "mpx.vmhba32:C0:T0:L0" not in ids  # PiKVM virtual CD-ROM
    kinds = {d.device_id: d.kind for d in disks}
    assert kinds["t10.ATA_____ST20000NM007D2D3DJ103________________________________ZVTBWXYS"] is DiskKind.HDD
    assert kinds["t10.ATA_____Samsung_SSD_750_EVO_120GB_______________S3F2NWBHB56997P_____"] is DiskKind.SSD
    assert kinds["t10.NVMe____EO000750KWTXC___________________________00014935E3E4D25C"] is DiskKind.NVME


def test_device_list_decodes_identity_from_t10_id():
    disks = {d.device_id: d for d in esxcli.parse_device_list(fixture_text(REAL + "esxcli_storage_core_device_list.txt"))}
    exos = disks["t10.ATA_____ST20000NM007D2D3DJ103________________________________ZVTBWXYS"]
    assert exos.serial == "ZVTBWXYS"
    assert exos.model == "ST20000NM007D-3DJ103"  # esxcli truncates it to ST20000NM007D-3D
    samsung = disks["t10.ATA_____Samsung_SSD_750_EVO_120GB_______________S3F2NWBHB56997P_____"]
    assert samsung.serial == "S3F2NWBHB56997P"
    assert samsung.model == "Samsung SSD 750 EVO 120GB"
    assert samsung.is_boot is True
    assert samsung.size_bytes == 114473 * 1024 * 1024
    nvme = disks["t10.NVMe____WD_BLACK_SN850X_4000GB__________________243E584E8B441B00"]
    assert nvme.model == "WD_BLACK SN850X 4000GB"
    assert nvme.serial == ""  # NVMe t10 ids carry an EUI-64, not the serial
    assert nvme.protocol == "nvme"


@pytest.mark.parametrize(
    ("device_id", "serial"),
    [
        ("t10.ATA_____CT4000MX500SSD1_________________________2323E6DF2809________", "2323E6DF2809"),
        ("t10.ATA_____ST20000NM007D2D3DJ103________________________________ZVTC3WC6", "ZVTC3WC6"),
    ],
)
def test_t10_serial(device_id, serial):
    assert esxcli.t10_serial(device_id) == serial


def test_nvme_adapter_table():
    assert esxcli.parse_nvme_adapters(fixture_text(REAL + "esxcli_nvme_device_list.txt")) == ["vmhba0", "vmhba1", "vmhba4"]


def test_nvme_smart_log_hex_and_kelvin():
    reading, raw = esxcli.parse_nvme_smart_log(fixture_text(REAL + "esxcli_nvme_device_log_smart_get_-A_vmhba4.txt"))
    assert reading.temperature_c == pytest.approx(38.9, abs=0.1)
    assert reading.power_on_hours == 0x63C6 == 25542
    assert reading.written_bytes == 0x52C46FC1 * 512 * 1000
    assert reading.read_bytes == 0x12F7CA12 * 512 * 1000
    assert reading.unsafe_shutdowns == 32
    assert reading.power_cycles == 47
    assert reading.media_errors == 0
    assert reading.life_used_pct == 0
    assert reading.available_spare_pct == 100
    assert reading.critical_warnings == []
    assert reading.health_passed is True
    assert raw["Controller Busy Time"] == "0x270f"


def test_nvme_smart_log_critical_warning():
    text = fixture_text(REAL + "esxcli_nvme_device_log_smart_get_-A_vmhba4.txt").replace(
        "Read Only Mode: false", "Read Only Mode: true"
    )
    reading, _ = esxcli.parse_nvme_smart_log(text)
    assert reading.critical_warnings == ["read_only"]
    assert reading.health_passed is False


def test_smartctl_seagate_hdd():
    reading, raw = parse_smartctl(fixture_text(REAL + "smartctl_seagate_exos_x20.json"))
    assert reading.health_passed is True
    assert reading.temperature_c == 30
    assert reading.temperature_limit_c == 60
    assert reading.power_on_hours == 23065
    assert reading.written_bytes == 175524780300 * 512
    assert reading.reallocated_sectors == 0
    assert reading.pending_sectors == 0
    assert reading.crc_errors == 0
    assert reading.life_used_pct is None  # spinning disk
    assert reading.available_spare_pct is None
    assert raw["serial_number"] == "ZVTBWXYS"
    command_timeout = next(a for a in raw["attributes"] if a["id"] == 188)
    assert command_timeout["raw"] == "0 0 0"


def test_smartctl_samsung_ssd_endurance():
    reading, raw = parse_smartctl(fixture_text(REAL + "smartctl_samsung_750_evo.json"))
    assert reading.life_used_pct == 23
    assert reading.life_remaining_pct == 77
    assert reading.written_bytes == 26147708707 * 512
    assert reading.power_on_hours == 80031
    assert reading.temperature_limit_c == 70
    assert reading.reported_uncorrectable == 0
    assert raw["model_name"] == "Samsung SSD 750 EVO 120GB"


def test_smartctl_endurance_falls_back_to_attributes():
    import json

    data = json.loads(fixture_text(REAL + "smartctl_samsung_750_evo.json"))
    del data["endurance_used"]
    reading, _ = parse_smartctl(json.dumps(data))
    assert reading.life_used_pct == 23  # 100 - normalized Wear_Leveling_Count


@pytest.mark.parametrize("name", ["smartctl_mx500_no_such_device.json", "smartctl_nvme_unable_to_detect.json"])
def test_smartctl_unreadable_device(name):
    with pytest.raises(SmartctlError):
        parse_smartctl(fixture_text(REAL + name))


def test_smartctl_standby_is_not_an_error_value():
    text = '{"smartctl": {"exit_status": 2, "messages": [{"string": "Device is in STANDBY mode, exit(2)"}]}}'
    with pytest.raises(SmartctlError, match="standby"):
        parse_smartctl(text)


def test_native_smart_table():
    # Synthetic sample in the documented layout, until a real capture exists.
    reading, rows = esxcli.parse_native_smart(fixture_text("synthetic/esxcli_storage_core_device_smart_get_ata.txt"))
    assert reading.health_passed is True
    assert reading.temperature_c == 30
    assert reading.life_used_pct is None
    assert rows["Reallocated Sector Count"] == {
        "Parameter": "Reallocated Sector Count",
        "Value": "0",
        "Threshold": "10",
        "Worst": "100",
    }


def test_path_list_maps_devices_to_adapters():
    mapping = esxcli.parse_path_list(fixture_text("synthetic/esxcli_storage_core_path_list.txt"))
    assert mapping["t10.NVMe____EO000750KWTXC___________________________00014935E3E4D25C"] == "vmhba4"
