import asyncio
import json

from conftest import fixture_text
from test_collector import EXOS, OPTANE, SAMSUNG, collect, sa_esxi_01

from vmware_disk_health import mqtt
from vmware_disk_health.config import MqttConfig, Settings
from vmware_disk_health.model import Severity


class FakeClient:
    def __init__(self):
        self.published: list[tuple[str, str, bool]] = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))

    def is_connected(self):
        return True

    def topics(self):
        return [topic for topic, _, _ in self.published]

    def payload(self, topic):
        return json.loads(next(p for t, p, _ in self.published if t == topic))


def collected():
    result, disks, _ = collect(sa_esxi_01(json=False, smartctl=False))
    return result, disks


def test_discovery_only_covers_values_the_drive_reports():
    _, disks = collected()
    config = MqttConfig()
    topics = {topic.split("/")[-2]: payload for topic, payload in mqtt.disk_messages(disks[EXOS], config)}
    assert "temperature_c" in topics and "power_on_hours" in topics
    # A hard disk has no endurance, and this one reports no CRC counter to esxcli.
    assert "life_used_pct" not in topics and "crc_errors" not in topics

    samsung = {topic: payload for topic, payload in mqtt.disk_messages(disks[SAMSUNG], config)}
    life = samsung["homeassistant/sensor/vdh_t10_ata_samsung_ssd_750_evo_120gb_s3f2nwbhb56997p/life_remaining_pct/config"]
    assert life["unit_of_measurement"] == "%" and life["state_class"] == "measurement"
    assert life["availability_topic"] == "vmware-disk-health/status"
    assert life["device"]["serial_number"] == "S3F2NWBHB56997P"
    assert life["device"]["via_device"] == "vmware_disk_health_host_sa_esxi_01"
    assert life["has_entity_name"] is True and life["name"] == "Life remaining"


def test_status_entity_is_an_enum_with_attributes():
    _, disks = collected()
    messages = dict(mqtt.disk_messages(disks[OPTANE], MqttConfig()))
    status = next(payload for topic, payload in messages.items() if topic.endswith("/status/config"))
    assert status["device_class"] == "enum"
    assert status["options"] == ["ok", "unknown", "warning", "critical"]
    assert status["json_attributes_topic"] == status["state_topic"]
    assert "entity_category" not in status  # the main state, not diagnostics


def test_state_payload():
    _, disks = collected()
    state = mqtt.disk_state(disks[SAMSUNG])
    assert state["status"] == "ok" and state["problem"] == "OFF"
    assert (state["temperature_c"], state["life_remaining_pct"], state["life_used_pct"]) == (37, 77, 23)
    assert state["serial"] == "S3F2NWBHB56997P" and state["findings"] == []

    warning = disks[SAMSUNG].model_copy(deep=True)
    warning.status = Severity.WARNING
    assert mqtt.disk_state(warning)["problem"] == "ON"
    assert mqtt.disk_state(warning, missing=True)["status"] == "unknown"


def test_publisher_announces_once_and_publishes_states():
    host, _ = collected()
    publisher = mqtt.MqttPublisher(Settings(), MqttConfig())
    publisher.client = FakeClient()
    asyncio.run(publisher.publish(host))
    first = len(publisher.client.published)
    assert "vmware-disk-health/host/sa_esxi_01/state" in publisher.client.topics()
    host_state = publisher.client.payload("vmware-disk-health/host/sa_esxi_01/state")
    assert host_state["reachable"] == "ON" and host_state["disks"] == 7
    assert publisher.client.payload("vmware-disk-health/disk/" + mqtt.slug(SAMSUNG) + "/state")["status"] == "ok"
    assert all(retain for _, _, retain in publisher.client.published)

    publisher.client.published.clear()
    asyncio.run(publisher.publish(host))
    # Second round: states again, but no repeated discovery configs.
    assert len(publisher.client.published) < first
    assert not any("homeassistant/" in topic for topic in publisher.client.topics())

    publisher._on_connect(publisher.client, None, None, 0)  # reconnect: announce again
    publisher.client.published.clear()
    asyncio.run(publisher.publish(host))
    assert any("homeassistant/" in topic for topic in publisher.client.topics())


def test_disk_override_renames_the_device():
    _, disks = collected()
    settings = Settings(disk_overrides=[{"match": "S3F2*", "name": "Boot SSD"}])
    publisher = mqtt.MqttPublisher(settings, MqttConfig())
    publisher.client = FakeClient()
    host, _ = collected()
    asyncio.run(publisher.publish(host))
    config = next(
        json.loads(p) for t, p, _ in publisher.client.published if t.endswith(f"vdh_{mqtt.slug(SAMSUNG)}/status/config")
    )
    assert config["device"]["name"] == "Boot SSD"


def test_supervisor_broker(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "token")
    payload = json.dumps({"data": {"host": "core-mosquitto", "port": 1883, "username": "addons", "password": "pw", "ssl": False}})

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return payload.encode()

    monkeypatch.setattr(mqtt.urllib.request, "urlopen", lambda *a, **k: Response())
    config = mqtt.supervisor_broker()
    assert (config.host, config.username, config.tls) == ("core-mosquitto", "addons", False)

    monkeypatch.delenv("SUPERVISOR_TOKEN")
    assert mqtt.supervisor_broker() is None


def test_fixtures_are_intact():
    assert "Data Units Written" in fixture_text("sa-esxi-01/esxcli_nvme_device_log_smart_get_-A_vmhba4.txt")
