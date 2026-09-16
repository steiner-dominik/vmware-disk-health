"""The publisher against a minimal MQTT broker, so the paho wiring is covered."""

import asyncio

from test_collector import sa_esxi_01

from vmware_disk_health.config import HostConfig, MqttConfig, Settings
from vmware_disk_health.mqtt import MqttPublisher
from vmware_disk_health.service import Integrations, Monitor
from vmware_disk_health.storage import Storage
from vmware_disk_health.transport import FixtureTransport, KeyStore

CONNACK = b"\x20\x02\x00\x00"


def varint(data: bytes, index: int) -> tuple[int, int]:
    value = multiplier = 0
    while True:
        byte = data[index]
        index += 1
        value += (byte & 0x7F) * multiplier if multiplier else (byte & 0x7F)
        multiplier = (multiplier or 1) * 128
        if not byte & 0x80:
            return value, index


class Broker:
    """Accepts a connection, answers CONNECT and collects published messages."""

    def __init__(self):
        self.messages: list[tuple[str, str, bool]] = []
        self.connected = asyncio.Event()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        buffer = b""
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            buffer += chunk
            while buffer:
                try:
                    length, start = varint(buffer, 1)
                except IndexError:
                    break
                if len(buffer) < start + length:
                    break
                packet, buffer = buffer[: start + length], buffer[start + length :]
                kind = packet[0] >> 4
                body = packet[start:]
                if kind == 1:  # CONNECT
                    writer.write(CONNACK)
                    await writer.drain()
                    self.connected.set()
                elif kind == 3:  # PUBLISH
                    topic_len = int.from_bytes(body[:2], "big")
                    topic = body[2 : 2 + topic_len].decode()
                    rest = body[2 + topic_len :]
                    qos = (packet[0] >> 1) & 3
                    if qos:
                        packet_id, rest = rest[:2], rest[2:]
                        writer.write(b"\x40\x02" + packet_id)  # PUBACK
                        await writer.drain()
                    self.messages.append((topic, rest.decode(errors="replace"), bool(packet[0] & 1)))
                elif kind == 12:  # PINGREQ
                    writer.write(b"\xd0\x00")
                    await writer.drain()
        writer.close()

    def topics(self) -> list[str]:
        return [topic for topic, _, _ in self.messages]


def test_publisher_talks_to_a_broker(tmp_path):
    async def scenario():
        broker = Broker()
        server = await asyncio.start_server(broker.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        config = MqttConfig(host="127.0.0.1", port=port)
        settings = Settings(mqtt=config, data_dir=tmp_path)

        async def connect(_host):
            return FixtureTransport(sa_esxi_01(json=False, smartctl=False))

        settings.hosts = [HostConfig(name="esx", address="10.0.0.1")]
        monitor = Monitor(settings, Storage(tmp_path / "h.db"), KeyStore(tmp_path), connect)
        publisher = MqttPublisher(settings, config)
        await publisher.start()
        await asyncio.wait_for(broker.connected.wait(), timeout=5)

        await publisher.publish((await monitor.poll())[0])
        await asyncio.sleep(0.4)
        await publisher.stop()
        server.close()
        return broker

    broker = asyncio.run(scenario())
    assert "vmware-disk-health/status" in broker.topics()
    assert ("vmware-disk-health/status", "online", True) in broker.messages
    assert ("vmware-disk-health/status", "offline", True) in broker.messages  # on shutdown
    assert any(t.startswith("homeassistant/binary_sensor/vdh_host_esx/") for t in broker.topics())
    state = next(payload for topic, payload, _ in broker.messages if topic == "vmware-disk-health/host/esx/state")
    assert '"reachable": "ON"' in state


def test_integrations_report_a_missing_broker(tmp_path):
    settings = Settings(data_dir=tmp_path)  # MQTT enabled, but no broker anywhere
    monitor = Monitor(settings, Storage(tmp_path / "h.db"), KeyStore(tmp_path), None)
    integrations = Integrations(settings, monitor)
    asyncio.run(integrations.start())
    status = integrations.status()
    assert status["mqtt_enabled"] and not status["mqtt_connected"]
    assert status["mqtt_error"] == "no broker configured"
    assert not monitor.listeners
