from vmware_disk_health.model import DiskInfo, DiskKind, DiskResult, Finding, HostResult, Reading, Severity
from vmware_disk_health.storage import Storage

DAY = 86400


def host_result(ts: float, status: Severity = Severity.OK, temperature: float = 30, reallocated: int = 0) -> HostResult:
    disk = DiskResult(
        host="esx",
        info=DiskInfo(device_id="t10.ATA_____X", serial="X", kind=DiskKind.HDD),
        reading=Reading(temperature_c=temperature, reallocated_sectors=reallocated),
        sources=["test"],
        status=status,
        findings=[Finding(code="t", severity=status, message="msg")] if status else [],
    )
    return HostResult(name="esx", address="10.0.0.1", ok=True, collected_at=ts, disks=[disk])


def test_save_tracks_latest_and_status_changes(tmp_path):
    storage = Storage(tmp_path / "h.db")
    first = storage.save(host_result(1000))
    assert [(c.old, c.new) for c in first] == [(None, Severity.OK)]
    assert storage.save(host_result(2000)) == []
    changed = storage.save(host_result(3000, Severity.WARNING, reallocated=3))
    assert [(c.old, c.new) for c in changed] == [(Severity.OK, Severity.WARNING)]
    assert storage.previous_reading("t10.ATA_____X").reallocated_sectors == 3
    assert len(storage.events()) == 2
    assert [row["ts"] for row in storage.history("t10.ATA_____X")] == [1000, 2000, 3000]


def test_failed_host_keeps_previous_disk_state(tmp_path):
    storage = Storage(tmp_path / "h.db")
    storage.save(host_result(1000))
    storage.save(HostResult(name="esx", address="10.0.0.1", ok=False, error="timeout", collected_at=2000))
    host = storage.hosts()[0]
    assert host["last_success"] == 1000 and host["last_error"] == "timeout"
    assert len(storage.disks()) == 1


def test_rollup_folds_old_samples_into_days(tmp_path):
    storage = Storage(tmp_path / "h.db")
    now = 200 * DAY
    for hour, temp in enumerate([20, 30, 40]):
        storage.save(host_result(10 * DAY + hour * 3600, temperature=temp))
    storage.save(host_result(now - 3600, temperature=35))
    assert storage.rollup(retention_days=90, now=now) == 3
    history = storage.history("t10.ATA_____X")
    assert len(history) == 2
    day = history[0]
    assert (day["temperature_min"], day["temperature_max"], day["temperature_c"]) == (20, 40, 30)
    assert history[1]["temperature_c"] == 35
