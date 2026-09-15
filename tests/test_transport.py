import asyncio

import asyncssh
import pytest

from vmware_disk_health.config import HostConfig
from vmware_disk_health.transport import HostKeyChangedError, KeyStore, SSHTransport


class _Server(asyncssh.SSHServer):
    def __init__(self, authorized: asyncssh.SSHKey):
        self._authorized = authorized

    def begin_auth(self, username: str) -> bool:
        return True

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        return key.public_data == self._authorized.public_data


async def _handle(process: asyncssh.SSHServerProcess) -> None:
    process.stdout.write(f"ran: {process.command}\n")
    process.exit(3 if "fail" in process.command else 0)


async def _serve(store: KeyStore):
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key = store.private_key()
    return await asyncssh.create_server(
        lambda: _Server(client_key), "127.0.0.1", 0, server_host_keys=[host_key], process_factory=_handle
    )


def test_ssh_pins_host_key_on_first_use_and_rejects_a_changed_one(tmp_path):
    async def scenario():
        store = KeyStore(tmp_path)
        server = await _serve(store)
        port = server.sockets[0].getsockname()[1]
        host = HostConfig(name="esx", address="127.0.0.1", port=port)

        transport = await SSHTransport.connect(host, store)
        result = await transport.run("esxcli storage core device list", timeout=5)
        assert result.ok and result.stdout == "ran: esxcli storage core device list\n"
        failed = await transport.run("fail", timeout=5)
        assert failed.exit_status == 3
        await transport.close()
        assert store.pinned(f"127.0.0.1:{port}")

        # Same key again: accepted.
        await (await SSHTransport.connect(host, store)).close()
        server.close()

        # Host reinstalled with a new key on the same address: refused.
        other = await _serve(store)
        host.port = other.sockets[0].getsockname()[1]
        store.pin(f"127.0.0.1:{host.port}", store.pinned(f"127.0.0.1:{port}"))
        with pytest.raises(HostKeyChangedError):
            await SSHTransport.connect(host, store)
        other.close()

    asyncio.run(scenario())


def test_key_store_generates_a_private_key_once(tmp_path):
    store = KeyStore(tmp_path)
    first = store.public_key()
    assert first.startswith("ssh-ed25519 ")
    assert store.public_key() == first
    assert oct(store.private_key_path.stat().st_mode)[-3:] == "600"
