"""vSAN tiers, TBW-based wear estimates and the fixes that came with them."""

import asyncio
import json

import pytest
from conftest import fixture_text
from test_collector import _disk

from vmware_disk_health import insights, metrics, mqtt
from vmware_disk_health.alerts import _send_gotify, format_change, should_notify
from vmware_disk_health.collector import ESXCLI_JSON, collect_host
from vmware_disk_health.config import AlertsConfig, HostConfig, MqttConfig, Settings
from vmware_disk_health.endurance import estimated_life_used, rating
from vmware_disk_health.evaluate import evaluate
from vmware_disk_health.model import DiskInfo, DiskKind, DiskResult, HostResult, Reading, Severity
from vmware_disk_health.parsers import esxcli
from vmware_disk_health.parsers.esxcli import Shape
from vmware_disk_health.storage import StatusChange, Storage
from vmware_disk_health.transport import CommandResult, FixtureTransport

VCF = "esx11-vcf/"
DAY = 86400
TB = 10**12
CACHE = "naa.55cd2e414dcf3635"
CAPACITY = "naa.55cd2e414d7a89c7"

# ------------------------------------------------------------------ endurance


@pytest.mark.parametrize(
    ("model", "size", "family", "tbw", "ambiguous"),
    [
        # The lab's drives, as esxcli names them.
        ("SSDSC2BB016T7R", 1600320962560, "Intel DC S3520", 2925, False),
        ("INTEL SSDSC2BA40", 400088367104, "Intel DC S3700 / Intel DC S3710", 7300, True),
        ("INTEL SSDSC2BB24", 240056795136, "Intel DC S3500 / Intel DC S3510 / Intel DC S3520", 140, True),
        # Full model codes pick the exact generation.
        ("INTEL SSDSC2BA400G4", 400088367104, "Intel DC S3710", 8300, False),
        ("CT1000MX500SSD1", 1000204886016, "Crucial MX500", 360, False),
    ],
)
def test_rating_lookup(model, size, family, tbw, ambiguous):
    found = rating(model, size, DiskKind.SSD)
    assert (found.family, found.tbw_bytes, found.ambiguous) == (family, tbw * TB, ambiguous)


def test_rating_unknown_or_not_applicable():
    assert rating("Samsung SSD 750 EVO 120GB", 120034123776, DiskKind.SSD) is None
    assert rating("SSDSC2BB016T7R", 1600320962560, DiskKind.HDD) is None
    assert rating("SSDSC2BB016T7R", None, DiskKind.SSD) is None
    # A capacity that is not in the family's table is not guessed.
    assert rating("CT1000MX500SSD1", 3 * TB, DiskKind.SSD) is None


def test_estimated_life_used():
    s3520 = rating("SSDSC2BB016T7R", 1600320962560, DiskKind.SSD)
    assert estimated_life_used(401564812443648, s3520) == pytest.approx(13.7, abs=0.1)
    assert estimated_life_used(None, s3520) is None


# ------------------------------------------------------------------ vSAN


def test_vsan_storage_list_text_equals_json():
    from_text = esxcli.parse_vsan_storage_list(
        esxcli.decode_text(fixture_text(VCF + "esxcli_vsan_storage_list.txt"), Shape.BLOCKS)
    )
    from_json = esxcli.parse_vsan_storage_list(
        esxcli.decode_json(fixture_text(VCF + "esxcli_json_vsan_storage_list.json"), Shape.BLOCKS)
    )
    assert from_text == from_json
    assert from_text[CACHE] == ("cache", CACHE)
    assert from_text[CAPACITY] == ("capacity", CACHE)


def vcf_host(*, vsan: bool) -> dict:
    responses = {
        f"{ESXCLI_JSON} system version get": json.dumps(
            {"Product": "VMware ESXi", "Version": "9.1.0", "Build": "Releasebuild-1"}
        ),
        f"{ESXCLI_JSON} storage core device list": fixture_text(VCF + "esxcli_json_storage_core_device_list_solidigm.json"),
        f"{ESXCLI_JSON} storage core device smart get -d {CAPACITY}": fixture_text(
            VCF + "esxcli_json_storage_core_device_smart_get_solidigm.json"
        ),
    }
    if vsan:
        responses[f"{ESXCLI_JSON} vsan storage list"] = fixture_text(VCF + "esxcli_json_vsan_storage_list.json")
    else:  # no vSAN: the namespace does not exist
        responses[f"{ESXCLI_JSON} vsan storage list"] = CommandResult("", "Error: Unknown command or namespace vsan", 1)
    return responses


def collect_vcf(*, vsan: bool) -> HostResult:
    host = HostConfig(name="esx11", address="10.0.0.11")
    transport = FixtureTransport(vcf_host(vsan=vsan))

    async def connect(_host):
        return transport

    return asyncio.run(collect_host(host, Settings(hosts=[host], data_dir="/tmp/unused"), connect, lambda key: (None, None)))


def test_collector_tags_vsan_tier_and_estimates_wear():
    result = collect_vcf(vsan=True)
    assert result.ok
    (disk,) = result.disks
    assert (disk.info.vsan_tier, disk.info.vsan_disk_group) == ("capacity", CACHE)
    # esxcli's wear value is unusable on this drive, so only an estimate exists.
    assert disk.reading.life_used_pct is None
    assert disk.endurance.family == "Intel DC S3520"
    assert disk.reading.life_used_estimated_pct == estimated_life_used(disk.reading.written_bytes, disk.endurance)
    assert disk.reading.life_remaining_estimated_pct > 80


def test_collector_without_vsan():
    (disk,) = collect_vcf(vsan=False).disks
    assert disk.info.vsan_tier is None and disk.info.vsan_disk_group is None


# ------------------------------------------------------------------ evaluation


def test_estimated_wear_warns_but_is_never_critical():
    t = Settings().thresholds
    worn = evaluate(_disk(DiskKind.SSD, life_used_estimated_pct=95), t)
    assert worn.status is Severity.WARNING and worn.findings[0].code == "life_low_estimated"
    assert evaluate(_disk(DiskKind.SSD, life_used_estimated_pct=150), t).status is Severity.WARNING
    assert evaluate(_disk(DiskKind.SSD, life_used_estimated_pct=40), t).status is Severity.OK
    # The drive's own value wins; the estimate is then ignored.
    assert evaluate(_disk(DiskKind.SSD, life_used_pct=10, life_used_estimated_pct=95), t).status is Severity.OK


def test_ssd_tolerates_a_small_stable_reallocated_count():
    t = Settings().thresholds
    assert evaluate(_disk(DiskKind.SSD, reallocated_sectors=1), t, baseline=Reading(reallocated_sectors=1)).status is Severity.OK
    assert evaluate(_disk(DiskKind.SSD, reallocated_sectors=9), t).status is Severity.OK
    many = evaluate(_disk(DiskKind.SSD, reallocated_sectors=10), t)
    assert many.status is Severity.WARNING and many.findings[0].code == "reallocated_sectors"
    rising = evaluate(_disk(DiskKind.SSD, reallocated_sectors=2), t, baseline=Reading(reallocated_sectors=1))
    assert rising.status is Severity.WARNING and rising.findings[0].code == "reallocated_sectors_rising"
    # Hard disks are unchanged: any reallocated sector warns.
    assert evaluate(_disk(DiskKind.HDD, reallocated_sectors=1), t).status is Severity.WARNING
    custom = Settings(thresholds={"ssd_reallocated_warn_count": 1}).thresholds
    assert evaluate(_disk(DiskKind.SSD, reallocated_sectors=1), custom).status is Severity.WARNING


# ------------------------------------------------------------------ insights


def test_unit_change_is_not_counted_as_writes():
    """The 26.09.22.1 unit fix made the counter jump by a factor of 65536."""
    history = [
        {"ts": 0, "written_bytes": 6_000_000_000},
        {"ts": 3600, "written_bytes": 6_001_000_000},
        {"ts": 7200, "written_bytes": 401 * TB},  # same drive, counter now in real bytes
        {"ts": 10800, "written_bytes": 401 * TB + 5_000_000_000},
    ]
    series = insights.written_per_day_series(history)
    assert sum(point["bytes"] for point in series) == 1_000_000 + 5_000_000_000


def test_tbw_projection_when_the_drive_reports_no_wear():
    s3520 = rating("SSDSC2BB016T7R", 1600320962560, DiskKind.SSD)
    written = 1000 * TB
    disk = DiskResult(
        host="h",
        info=DiskInfo(device_id="d", kind=DiskKind.SSD),
        reading=Reading(written_bytes=written, power_on_hours=24 * 1000, life_used_estimated_pct=34.2),
        endurance=s3520,
    )
    now = 1_000_000_000
    out = insights.compute(disk, [], now)
    # 1 TB/day lifetime average, 1925 TB of rating left.
    assert out.life_estimated and out.life_basis == "lifetime"
    assert out.life_end_ts == pytest.approx(now + 1925 * DAY)


# ------------------------------------------------------------------ storage


def ssd_host(ts: float, written: int | None, *, disks: int = 1, life_used: float | None = None) -> HostResult:
    results = [
        DiskResult(
            host="esx",
            info=DiskInfo(device_id=f"naa.{i}", model="SSDSC2BB016T7R", kind=DiskKind.SSD, size_bytes=1600320962560),
            reading=Reading(written_bytes=written, read_bytes=written, life_used_pct=life_used),
            sources=["test"],
            status=Severity.OK,
        )
        for i in range(disks)
    ]
    return HostResult(name="esx", address="10.0.0.1", ok=True, collected_at=ts, disks=results)


def test_history_in_the_old_unit_is_repaired_on_upgrade(tmp_path):
    path = tmp_path / "h.db"
    storage = Storage(path)
    units = 11_967_000  # 32 MiB units, recorded as 512-byte sectors before the fix
    storage.save(ssd_host(1 * DAY, units * 512, life_used=0.0))
    storage.save(ssd_host(2 * DAY, (units + 3) * 512, life_used=0.0))
    storage.save(ssd_host(3 * DAY, (units + 6) * 32 * 1024 * 1024))  # after the fix
    storage.db.execute("PRAGMA user_version=1")  # as written by 26.09.22.3
    storage.db.commit()
    storage.close()

    history = Storage(path).history("naa.0")
    assert [p["written_bytes"] for p in history] == [(units + n) * 32 * 1024 * 1024 for n in (0, 3, 6)]
    assert [p["read_bytes"] for p in history] == [p["written_bytes"] for p in history]
    assert all(p["life_used_pct"] is None for p in history)  # the stuck "0 % used" is gone


def test_new_columns_are_added_to_old_databases(tmp_path):
    path = tmp_path / "h.db"
    storage = Storage(path)
    for table in ("samples", "daily"):
        storage.db.execute(f"ALTER TABLE {table} DROP COLUMN life_used_estimated_pct")
    storage.db.commit()
    storage.close()
    storage = Storage(path)
    storage.save(ssd_host(DAY, 10 * TB))
    assert "life_used_estimated_pct" in storage.history("naa.0")[0]


def test_disappearing_disk_is_an_event_and_its_return_too(tmp_path):
    storage = Storage(tmp_path / "h.db")
    assert storage.save(ssd_host(1 * DAY, TB, disks=2)) == []
    [gone] = storage.save(ssd_host(2 * DAY, TB, disks=1))
    assert (gone.kind, gone.disk.key, gone.new) == ("missing", "naa.1", Severity.UNKNOWN)
    assert storage.save(ssd_host(3 * DAY, TB, disks=1)) == []  # reported once, not every poll
    [back] = storage.save(ssd_host(4 * DAY, TB, disks=2))
    assert (back.kind, back.disk.key) == ("returned", "naa.1")
    codes = [event["findings"][0]["code"] for event in storage.events()]
    assert codes == ["disk_returned", "disk_missing"]


def test_excluded_disk_is_not_reported_missing(tmp_path):
    storage = Storage(tmp_path / "h.db")
    storage.save(ssd_host(1 * DAY, TB, disks=2))
    host = ssd_host(2 * DAY, TB, disks=1)
    host.excluded = ["naa.1"]
    assert storage.save(host) == []


# ------------------------------------------------------------------ outputs


def test_missing_disk_notifications():
    disk = ssd_host(1, TB).disks[0]
    config = AlertsConfig(enabled=True)
    missing = StatusChange(disk, Severity.OK, Severity.UNKNOWN, "missing")
    assert should_notify(missing, config)
    assert format_change(missing)[0].startswith("Missing:")
    assert not should_notify(missing, AlertsConfig(enabled=True, min_severity="critical"))
    returned = StatusChange(disk, Severity.UNKNOWN, Severity.OK, "returned")
    assert should_notify(returned, config) and format_change(returned)[0].startswith("Back:")


def test_gotify_token_is_sent_as_header(monkeypatch):
    posted = []
    monkeypatch.setattr("vmware_disk_health.alerts._post", lambda url, data, headers: posted.append((url, headers)))
    _send_gotify(AlertsConfig(gotify_url="https://gotify.lan/", gotify_token="secret"), "t", "b", Severity.WARNING)
    [(url, headers)] = posted
    assert url == "https://gotify.lan/message" and headers["X-Gotify-Key"] == "secret"


def test_mqtt_host_state_when_unreachable():
    down = HostResult(name="esx", address="x", ok=False, error="timeout")
    state = mqtt.host_state(down, [], last_success=1_000_000)
    assert state["reachable"] == "OFF"
    assert state["disks"] is None and state["problem_disks"] is None  # unknown, not zero
    assert state["last_success"].startswith("1970-01-12")


def test_mqtt_missing_disk_and_new_entities():
    disk = ssd_host(1, TB).disks[0]
    disk.info.vsan_tier = "capacity"
    disk.reading.life_used_estimated_pct = 20.0
    state = mqtt.disk_state(disk, missing=True)
    assert (state["status"], state["problem"], state["vsan_tier"]) == ("unknown", "ON", "capacity")
    assert state["life_remaining_estimated_pct"] == 80.0
    topics = [topic for topic, _ in mqtt.disk_messages(disk, MqttConfig())]
    assert any(topic.endswith("/life_remaining_estimated_pct/config") for topic in topics)


def test_mqtt_client_ids_differ_between_deployments():
    ids = {mqtt.MqttPublisher(Settings(), MqttConfig(host="b", base_topic=base))._build()._client_id for base in ("home", "lab")}
    assert len(ids) == 2


def test_metrics_for_missing_vsan_and_estimates():
    present, gone = ssd_host(1, TB, disks=2).disks
    present.info.vsan_tier, present.info.vsan_disk_group = "cache", "naa.0"
    present.reading.life_used_estimated_pct = 12.5
    text = metrics.render([], [present, gone], missing={gone.key})
    assert 'vmware_disk_health_disk_vsan_info{host="esx",device="naa.0",tier="cache",disk_group="naa.0"} 1' in text
    assert "vmware_disk_health_disk_life_used_estimated_percent{" in text
    assert 'disk_status{host="esx",device="naa.1"' in text
    assert 'written_bytes_total{host="esx",device="naa.1"' not in text  # no stale values
