"""Running commands on ESXi hosts: SSH with a pinned host key, or canned output for tests."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import asyncssh

from .config import HostConfig

log = logging.getLogger(__name__)


@dataclass
class CommandResult:
    stdout: str
    stderr: str = ""
    exit_status: int = 0

    @property
    def ok(self) -> bool:
        return self.exit_status == 0


class Transport(Protocol):
    async def run(self, command: str, timeout: float) -> CommandResult: ...

    async def close(self) -> None: ...


class HostKeyChangedError(Exception):
    pass


class KeyStore:
    """The app's own SSH identity and the host keys it has pinned."""

    def __init__(self, data_dir: Path):
        self.dir = data_dir / "ssh"
        self.private_key_path = self.dir / "id_ed25519"
        self.known_hosts_path = self.dir / "known_hosts.json"

    def private_key(self) -> asyncssh.SSHKey:
        if not self.private_key_path.exists():
            self.dir.mkdir(parents=True, exist_ok=True)
            key = asyncssh.generate_private_key("ssh-ed25519", comment="vmware-disk-health")
            key.write_private_key(self.private_key_path)
            self.private_key_path.chmod(0o600)
            log.info("generated SSH key %s", self.private_key_path)
        return asyncssh.read_private_key(self.private_key_path)

    def public_key(self) -> str:
        return self.private_key().export_public_key().decode().strip()

    def _known(self) -> dict[str, str]:
        if not self.known_hosts_path.exists():
            return {}
        return json.loads(self.known_hosts_path.read_text())

    def pinned(self, host_id: str) -> str | None:
        return self._known().get(host_id)

    def pin(self, host_id: str, key: str) -> None:
        known = self._known()
        known[host_id] = key
        self.dir.mkdir(parents=True, exist_ok=True)
        self.known_hosts_path.write_text(json.dumps(known, indent=2))

    def forget(self, host_id: str) -> bool:
        known = self._known()
        if known.pop(host_id, None) is None:
            return False
        self.known_hosts_path.write_text(json.dumps(known, indent=2))
        return True


def host_id(host: HostConfig) -> str:
    return f"{host.address}:{host.port}"


class _PinningClient(asyncssh.SSHClient):
    """Trust the host key on first use, refuse any different key afterwards."""

    def __init__(self, store: KeyStore, hid: str):
        self._store = store
        self._hid = hid
        self.rejected: str | None = None

    def validate_host_public_key(self, host: str, addr: str, port: int, key: asyncssh.SSHKey) -> bool:
        presented = key.export_public_key().decode().strip()
        pinned = self._store.pinned(self._hid)
        if pinned is None:
            self._store.pin(self._hid, presented)
            log.info("pinned host key for %s: %s", self._hid, key.get_fingerprint())
            return True
        if pinned.split()[:2] == presented.split()[:2]:
            return True
        self.rejected = key.get_fingerprint()
        return False


class SSHTransport:
    def __init__(self, conn: asyncssh.SSHClientConnection):
        self._conn = conn

    @classmethod
    async def connect(cls, host: HostConfig, store: KeyStore, timeout: float = 20) -> SSHTransport:
        hid = host_id(host)
        client = _PinningClient(store, hid)
        try:
            conn = await asyncssh.connect(
                host.address,
                port=host.port,
                username=host.username,
                password=host.password,
                client_keys=[store.private_key()],
                agent_path=None,
                # Empty trust lists instead of None, so every key goes through
                # the pinning client; None would disable verification entirely.
                known_hosts=([], [], []),
                client_factory=lambda: client,
                connect_timeout=timeout,
                login_timeout=timeout,
            )
        except (asyncssh.HostKeyNotVerifiable, asyncssh.KeyExchangeFailed) as exc:
            if client.rejected:
                raise HostKeyChangedError(
                    f"host key of {hid} changed (now {client.rejected}); if the host was reinstalled, "
                    "forget the old key and reconnect"
                ) from exc
            raise
        return cls(conn)

    async def run(self, command: str, timeout: float) -> CommandResult:
        result = await self._conn.run(command, check=False, timeout=timeout)
        return CommandResult(
            stdout=str(result.stdout or ""),
            stderr=str(result.stderr or ""),
            exit_status=result.exit_status if result.exit_status is not None else -1,
        )

    async def close(self) -> None:
        self._conn.close()
        await self._conn.wait_closed()


class FixtureTransport:
    """Replays recorded output; unknown commands fail like a missing binary."""

    def __init__(self, responses: dict[str, CommandResult | str]):
        self.responses = {k: v if isinstance(v, CommandResult) else CommandResult(v) for k, v in responses.items()}
        self.commands: list[str] = []

    async def run(self, command: str, timeout: float) -> CommandResult:
        self.commands.append(command)
        await asyncio.sleep(0)
        return self.responses.get(command, CommandResult("", f"sh: {command.split()[0]}: not found", 127))

    async def close(self) -> None:
        return None
