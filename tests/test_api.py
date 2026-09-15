import asyncio
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from test_collector import EXOS, OPTANE, SAMSUNG, sa_esxi_01

from vmware_disk_health.api import create_app
from vmware_disk_health.config import HostConfig, Settings
from vmware_disk_health.insights import compute, written_per_day_series
from vmware_disk_health.model import DiskInfo, DiskKind, DiskResult, Reading
from vmware_disk_health.service import Monitor
from vmware_disk_health.storage import Storage
from vmware_disk_health.transport import FixtureTransport, KeyStore


@pytest.fixture
def client(tmp_path):
    host = HostConfig(name="sa-esxi-01", address="10.0.0.1")
    settings = Settings(hosts=[host], data_dir=tmp_path)

    async def connect(_host):
        return FixtureTransport(sa_esxi_01(json=True, smartctl=False))

    monitor = Monitor(settings, Storage(tmp_path / "history.db"), KeyStore(tmp_path), connect)
    asyncio.run(monitor.poll())
    with TestClient(create_app(settings, monitor, start_scheduler=False)) as test_client:
        yield test_client


def test_state_lists_hosts_and_disks(client):
    state = client.get("/api/state").json()
    [host] = state["hosts"]
    assert host["last_success"] and host["esxcli_json"] and host["smartctl_path"] is None
    assert host["esxi_version"] == "VMware ESXi 8.0.3 Update 3 build-25205845"
    assert len(state["disks"]) == 7
    assert state["counts"] == {"ok": 3, "unknown": 4, "warning": 0, "critical": 0}  # 4 without captured SMART output
    samsung = next(d for d in state["disks"] if d["key"] == SAMSUNG)
    assert (samsung["life_remaining_pct"], samsung["temperature_c"], samsung["is_boot"]) == (77, 37, True)


def test_disk_detail_and_history(client):
    detail = client.get(f"/api/disks/{quote(OPTANE, safe='')}").json()
    assert detail["disk"]["info"]["serial"] == "PHKE018000T8750BGN"
    assert detail["temperature_crit_c"] == pytest.approx(69.9, abs=0.1)
    # Written rate from the lifetime average until a week of history exists.
    assert detail["insights"]["written_basis"] == "lifetime"
    raw = detail["disk"]["raw"]["esxcli_nvme"]
    assert raw["_labels"]["dataunitswritten"] == "DataUnitsWritten"

    history = client.get(f"/api/disks/{quote(EXOS, safe='')}/history?days=7").json()
    assert len(history["points"]) == 1 and history["points"][0]["temperature_c"] == 30
    assert client.get("/api/disks/nope").status_code == 404


def test_metrics(client):
    text = client.get("/metrics").text
    assert 'vmware_disk_health_host_up{host="sa-esxi-01"} 1' in text
    assert "# TYPE vmware_disk_health_disk_written_bytes_total counter" in text
    assert f'vmware_disk_health_disk_temperature_celsius{{host="sa-esxi-01",device="{SAMSUNG}"' in text


def test_setup_and_security(client):
    setup = client.get("/api/setup").json()
    assert setup["public_key"].startswith("ssh-ed25519 ")
    assert setup["authorize_command"].endswith(">> /etc/ssh/keys-root/authorized_keys")
    # Resetting a pinned host key over an unauthenticated standalone UI is refused.
    assert client.post("/api/hosts/sa-esxi-01/forget-host-key").status_code == 403
    assert client.post("/api/hosts/unknown/test").status_code == 404


def test_ui_is_served_with_relative_asset_paths(client):
    page = client.get("/").text
    assert 'src="static/app.js"' in page and 'href="static/app.css"' in page
    asset = client.get("/static/app.js")
    assert asset.status_code == 200 and asset.headers["cache-control"] == "no-cache"


def test_healthz_reflects_scheduler(client):
    # Scheduler not started in this fixture: the watchdog must see that.
    assert client.get("/healthz").status_code == 503


def test_insights_projection_from_history():
    day = 86400
    disk = DiskResult(
        host="h",
        info=DiskInfo(device_id="d", kind=DiskKind.SSD),
        reading=Reading(life_used_pct=12, written_bytes=100 * 10**12, power_on_hours=20000),
        sources=["t"],
    )
    now = 400 * day
    history = [
        {"ts": now - 300 * day, "written_bytes": 70 * 10**12, "life_used_pct": 10},
        {"ts": now - 10 * day, "written_bytes": 99 * 10**12, "life_used_pct": 12},
        {"ts": now, "written_bytes": 100 * 10**12, "life_used_pct": 12},
    ]
    result = compute(disk, history, now)
    assert result.written_basis == "history"
    assert result.written_per_day_bytes == pytest.approx(10**11)  # 1 TB in the last 10 days
    assert result.life_basis == "history"
    assert result.life_used_per_year_pct == pytest.approx(2 / (300 / 365.25))
    assert result.life_end_ts > now

    series = written_per_day_series(history)
    assert [round(p["bytes"] / 10**12) for p in series] == [29, 1]
