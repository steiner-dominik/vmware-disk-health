import asyncio

from test_collector import _disk

from vmware_disk_health.alerts import Notifier, format_change, should_notify
from vmware_disk_health.config import AlertsConfig, Settings
from vmware_disk_health.evaluate import evaluate
from vmware_disk_health.model import DiskKind, HostResult, Severity
from vmware_disk_health.storage import StatusChange


def change(old, new, **values):
    disk = evaluate(_disk(DiskKind.SSD, **values), Settings().thresholds)
    disk.status = new
    return StatusChange(disk, old, new)


def test_should_notify_respects_the_floor():
    warn_and_up = AlertsConfig(enabled=True, min_severity="warning")
    critical_only = AlertsConfig(enabled=True, min_severity="critical")
    assert should_notify(change(Severity.OK, Severity.WARNING), warn_and_up)
    assert not should_notify(change(Severity.OK, Severity.WARNING), critical_only)
    assert should_notify(change(Severity.WARNING, Severity.CRITICAL), critical_only)
    # Recovery only after something worth alerting on.
    assert should_notify(change(Severity.WARNING, Severity.OK), warn_and_up)
    assert not should_notify(change(Severity.UNKNOWN, Severity.OK), warn_and_up)
    assert not should_notify(change(Severity.WARNING, Severity.OK), AlertsConfig(enabled=True, notify_recovery=False))


def test_message_text():
    title, body = format_change(change(Severity.OK, Severity.CRITICAL, pending_sectors=3))
    assert title.startswith("Critical:")
    assert "3 pending sectors" in body
    title, _ = format_change(change(Severity.CRITICAL, Severity.OK))
    assert title.startswith("Recovered:")


def test_notifier_sends_and_tracks_unreachable_hosts(monkeypatch):
    sent = []
    for name in ("_send_ntfy", "_send_gotify", "_send_mail"):
        monkeypatch.setattr(f"vmware_disk_health.alerts.{name}", lambda c, t, b, s, n=name: sent.append((n, t)))
    notifier = Notifier(AlertsConfig(enabled=True, ntfy_url="https://ntfy.sh/test"))
    assert notifier.configured

    host = HostResult(name="esx", address="10.0.0.1", ok=True)
    asyncio.run(notifier.on_results(host, [change(Severity.OK, Severity.CRITICAL, pending_sectors=1)]))
    assert {name for name, _ in sent} == {"_send_ntfy", "_send_gotify", "_send_mail"}  # all channels, any order

    sent.clear()
    down = HostResult(name="esx", address="10.0.0.1", ok=False, error="timeout")
    asyncio.run(notifier.on_results(down, []))
    assert "Host unreachable: esx" in [title for _, title in sent]
    sent.clear()
    asyncio.run(notifier.on_results(down, []))  # still down: no repeat
    assert not sent
    asyncio.run(notifier.on_results(host, []))
    assert "Host reachable again: esx" in [title for _, title in sent]


def test_nothing_is_sent_without_a_channel():
    notifier = Notifier(AlertsConfig(enabled=True))
    assert not notifier.configured
    asyncio.run(notifier.on_results(HostResult(name="esx", address="x", ok=False), []))
