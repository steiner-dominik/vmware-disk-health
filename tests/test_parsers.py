import json

import pytest
from conftest import fixture_text

from vmware_disk_health.model import DiskKind
from vmware_disk_health.parsers import esxcli
from vmware_disk_health.parsers.esxcli import Shape
from vmware_disk_health.parsers.smartctl import SmartctlError, parse_smartctl

SA = "sa-esxi-01/"
SC = "sc-esxi-01/"


def text(name: str, shape: Shape):
    return esxcli.decode_text(fixture_text(name), shape)


def device_list():
    return esxcli.parse_device_list(text(SA + "esxcli_storage_core_device_list.txt", Shape.BLOCKS))


def test_device_list_keeps_local_physical_disks_only():
    disks = device_list()
    assert len(disks) == 7
    assert "mpx.vmhba32:C0:T0:L0" not in [d.device_id for d in disks]  # PiKVM virtual CD-ROM
    kinds = {d.device_id: d.kind for d in disks}
    assert kinds["t10.ATA_____ST20000NM007D2D3DJ103________________________________ZVTBWXYS"] is DiskKind.HDD
    assert kinds["t10.ATA_____Samsung_SSD_750_EVO_120GB_______________S3F2NWBHB56997P_____"] is DiskKind.SSD
    assert kinds["t10.NVMe____EO000750KWTXC___________________________00014935E3E4D25C"] is DiskKind.NVME


def test_device_list_decodes_identity_from_t10_id():
    disks = {d.device_id: d for d in device_list()}
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


def test_device_list_from_json_records():
    # Same parser, records shaped like the JSON formatter's keys.
    records = [
        {
            "Device": "t10.ATA_____CT4000MX500SSD1_________________________2323E6DF2809________",
            "DeviceType": "Direct-Access ",
            "DevfsPath": "/vmfs/devices/disks/t10.ATA_____CT4000MX500SSD1_________________________2323E6DF2809________",
            "IsLocal": True,
            "IsUSB": False,
            "IsSSD": True,
            "Model": "CT4000MX500SSD1 ",
            "Vendor": "ATA     ",
            "Size": 3815447,
        }
    ]
    [disk] = esxcli.parse_device_list(esxcli.decode_json(json.dumps(records), Shape.BLOCKS))
    assert (disk.kind, disk.serial, disk.model) == (DiskKind.SSD, "2323E6DF2809", "CT4000MX500SSD1")


@pytest.mark.parametrize(
    ("device_id", "serial"),
    [
        ("t10.ATA_____CT4000MX500SSD1_________________________2323E6DF2809________", "2323E6DF2809"),
        ("t10.ATA_____ST20000NM007D2D3DJ103________________________________ZVTC3WC6", "ZVTC3WC6"),
    ],
)
def test_t10_serial(device_id, serial):
    assert esxcli.t10_serial(device_id) == serial


def test_version():
    record = esxcli.decode_json(fixture_text(SA + "esxcli_json_system_version_get.json"), Shape.RECORD)
    assert esxcli.parse_version(record) == "VMware ESXi 8.0.3 Update 3 build-25205845"
    assert esxcli.parse_vmware_vl(fixture_text(SC + "vmware_-vl.txt")) == "VMware ESXi 8.0.3 build-25595708"


def test_path_list_maps_nvme_devices_to_adapters():
    mapping = esxcli.parse_path_list(text(SA + "esxcli_storage_core_path_list.txt", Shape.BLOCKS))
    assert mapping["t10.NVMe____EO000750KWTXC___________________________00014935E3E4D25C"] == "vmhba4"
    assert mapping["t10.NVMe____WD_BLACK_SN850X_4000GB__________________4695514E8B441B00"] == "vmhba0"
    assert mapping["t10.NVMe____WD_BLACK_SN850X_4000GB__________________243E584E8B441B00"] == "vmhba1"


def test_nvme_adapter_table():
    records = text(SA + "esxcli_nvme_device_list.txt", Shape.TABLE)
    assert esxcli.parse_nvme_adapters(records) == ["vmhba0", "vmhba1", "vmhba4"]


def test_nvme_device_get_serial_and_kelvin_threshold():
    record = text(SA + "esxcli_nvme_device_get_-A_vmhba4.txt", Shape.RECORD)
    serial, limit = esxcli.parse_nvme_device_get(record)
    assert serial == "PHKE018000T8750BGN"
    assert limit == pytest.approx(69.9, abs=0.1)  # 343 K; critical threshold is 0 = not set


def test_nvme_smart_log_hex_and_kelvin():
    reading = esxcli.parse_nvme_smart_log(text(SA + "esxcli_nvme_device_log_smart_get_-A_vmhba4.txt", Shape.RECORD))
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


def test_nvme_smart_log_json_equals_text():
    from_text = esxcli.parse_nvme_smart_log(text(SA + "esxcli_nvme_device_log_smart_get_-A_vmhba4.txt", Shape.RECORD))
    from_json = esxcli.parse_nvme_smart_log(
        esxcli.decode_json(fixture_text(SA + "esxcli_json_nvme_device_log_smart_get_-A_vmhba4.json"), Shape.RECORD)
    )
    assert from_json == from_text


def test_nvme_smart_log_critical_warning():
    record = esxcli.decode_json(fixture_text(SA + "esxcli_json_nvme_device_log_smart_get_-A_vmhba4.json"), Shape.RECORD)
    record["readonlymode"] = True
    reading = esxcli.parse_nvme_smart_log(record)
    assert reading.critical_warnings == ["read_only"]
    assert reading.health_passed is False


def native(name: str, kind: DiskKind, block: int | None = None):
    return esxcli.parse_native_smart(text(name, Shape.TABLE), kind, block)


def test_native_smart_uses_raw_values_for_ata():
    exos = native(SA + "esxcli_storage_core_device_smart_get_exos.txt", DiskKind.HDD)
    assert exos.health_passed is True
    assert exos.temperature_c == 30  # low byte of 38654705694
    assert exos.power_on_hours == 23065  # not the normalized 74
    assert exos.written_bytes == 175524784828 * 512
    assert exos.reallocated_sectors == 0
    assert exos.pending_sectors == 0
    assert exos.offline_uncorrectable == 0
    assert exos.life_used_pct is None
    assert exos.failing_attributes == []


def test_native_smart_normalized_temperature_is_not_used():
    samsung = native(SA + "esxcli_storage_core_device_smart_get_samsung.txt", DiskKind.SSD)
    assert samsung.temperature_c == 37  # the Value column says 63
    assert samsung.life_used_pct == 23  # Media Wearout Indicator 77
    assert samsung.power_on_hours == 80031
    mx500 = native(SC + "esxcli_storage_core_device_smart_get_mx500.txt", DiskKind.SSD)
    assert mx500.temperature_c == 36  # Value 64, raw 219043332132 = 36 (Min/Max 0/51)
    assert mx500.life_used_pct is None  # the MX500 reports no wearout indicator to ESXi


def test_native_smart_nvme_rows_have_no_raw_column_values():
    optane = native(SA + "esxcli_storage_core_device_smart_get_optane.txt", DiskKind.NVME)
    assert optane.temperature_c == 39
    assert optane.power_on_hours == 25542
    assert optane.reallocated_sectors is None
    assert optane.failing_attributes == []  # "Reallocated Sector Count 0 / threshold 100" is not normalized


def test_native_smart_respects_logical_block_size():
    exos = native(SA + "esxcli_storage_core_device_smart_get_exos.txt", DiskKind.HDD, block=4096)
    assert exos.written_bytes == 175524784828 * 4096


def test_native_smart_attribute_below_threshold():
    rows = text(SA + "esxcli_storage_core_device_smart_get_exos.txt", Shape.TABLE)
    reallocated = next(r for r in rows if r["parameter"] == "Reallocated Sector Count")
    reallocated["value"] = "9"  # threshold 10
    assert esxcli.parse_native_smart(rows, DiskKind.HDD).failing_attributes == ["Reallocated Sector Count"]


def test_capacity_list():
    # Synthetic sample in the documented layout, until a real capture exists.
    capacities = esxcli.parse_capacity_list(text("synthetic/esxcli_storage_core_device_capacity_list.txt", Shape.TABLE))
    assert capacities["t10.ATA_____ST20000NM007D2D3DJ103________________________________ZVTBWXYS"] == (512, "512e")


def test_smartctl_seagate_hdd():
    reading, raw = parse_smartctl(fixture_text(SA + "smartctl_seagate_exos_x20.json"))
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
    reading, raw = parse_smartctl(fixture_text(SA + "smartctl_samsung_750_evo.json"))
    assert reading.life_used_pct == 23
    assert reading.life_remaining_pct == 77
    assert reading.written_bytes == 26147708707 * 512
    assert reading.power_on_hours == 80031
    assert reading.temperature_limit_c == 70
    assert raw["model_name"] == "Samsung SSD 750 EVO 120GB"


def test_smartctl_crucial_mx500():
    reading, _ = parse_smartctl(fixture_text(SC + "smartctl_crucial_mx500.json"))
    assert reading.life_used_pct == 1
    assert reading.written_bytes == 27206376560 * 512
    assert reading.temperature_c == 35
    assert reading.temperature_limit_c == 70  # op_limit_max, not limit_max 100
    assert reading.reallocated_sectors == 0  # id 5 is named Reallocate_NAND_Blk_Cnt here
    assert reading.pending_sectors == 0
    assert reading.failing_attributes == []


def test_smartctl_endurance_falls_back_to_attributes():
    data = json.loads(fixture_text(SA + "smartctl_samsung_750_evo.json"))
    del data["endurance_used"]
    reading, _ = parse_smartctl(json.dumps(data))
    assert reading.life_used_pct == 23  # 100 - normalized Wear_Leveling_Count

    data = json.loads(fixture_text(SC + "smartctl_crucial_mx500.json"))
    del data["endurance_used"]
    del data["ata_device_statistics"]
    reading, _ = parse_smartctl(json.dumps(data))
    assert reading.life_used_pct == 1  # raw Percent_Lifetime_Remain


def test_smartctl_failing_attribute():
    data = json.loads(fixture_text(SA + "smartctl_seagate_exos_x20.json"))
    next(a for a in data["ata_smart_attributes"]["table"] if a["id"] == 5)["when_failed"] = "now"
    reading, _ = parse_smartctl(json.dumps(data))
    assert reading.failing_attributes == ["Reallocated_Sector_Ct"]


@pytest.mark.parametrize("name", ["smartctl_mx500_no_such_device.json", "smartctl_nvme_unable_to_detect.json"])
def test_smartctl_unreadable_device(name):
    with pytest.raises(SmartctlError):
        parse_smartctl(fixture_text(SA + name))


def test_smartctl_standby_is_reported_as_skipped():
    output = '{"smartctl": {"exit_status": 2, "messages": [{"string": "Device is in STANDBY mode, exit(2)"}]}}'
    with pytest.raises(SmartctlError, match="standby"):
        parse_smartctl(output)


def test_unexpected_json_is_rejected():
    with pytest.raises(ValueError):
        esxcli.decode_json('"just a string"', Shape.TABLE)
    with pytest.raises(ValueError):
        esxcli.decode_json("Unable to find requested formatter: json", Shape.RECORD)
