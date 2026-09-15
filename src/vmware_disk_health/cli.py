"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from .collector import collect_all
from .config import HostConfig, Settings, load_settings
from .evaluate import RISING_WINDOW_DAYS
from .model import HostResult
from .storage import Storage
from .transport import KeyStore, SSHTransport, host_id


def _tb(value: int | None) -> str:
    return "-" if value is None else f"{value / 1e12:,.1f} TB"


def print_table(results: list[HostResult]) -> None:
    for host in results:
        if not host.ok:
            print(f"\n{host.name} ({host.address}): FAILED - {host.error}")
            continue
        smartctl = host.smartctl_path or "not installed"
        print(f"\n{host.name} ({host.address}) - {host.esxi_version} - smartctl: {smartctl} - {host.duration_s}s")
        for disk in host.disks:
            r = disk.reading
            life = "-" if r.life_remaining_pct is None else f"{r.life_remaining_pct:.0f}%"
            temp = "-" if r.temperature_c is None else f"{r.temperature_c:.0f}°C"
            print(
                f"  [{disk.status.label:8}] {disk.info.kind.value:4} {disk.info.model[:28]:28} {disk.info.serial[:20]:20} "
                f"temp {temp:>5}  life {life:>4}  written {_tb(r.written_bytes):>10}  "
                f"POH {r.power_on_hours if r.power_on_hours is not None else '-':>6}  via {','.join(disk.sources) or '-'}"
            )
            for finding in disk.findings:
                print(f"      {finding.severity.label}: {finding.message}")
            for error in disk.errors:
                print(f"      note: {error}")


async def _collect(settings: Settings, args: argparse.Namespace) -> int:
    if args.host:
        settings.hosts = [h for h in settings.hosts if h.name == args.host]
        if not settings.hosts:
            print(f"no host named {args.host!r} in the configuration", file=sys.stderr)
            return 2
    store = KeyStore(settings.data_dir)
    storage = None if args.no_store else Storage(settings.data_dir / "history.db")

    async def connect(host: HostConfig) -> SSHTransport:
        return await SSHTransport.connect(host, store)

    since = time.time() - RISING_WINDOW_DAYS * 86400

    def baseline(key: str):
        return storage.baseline_reading(key, since) if storage else None

    results = await collect_all(settings, connect, baseline)
    if storage:
        for host in results:
            storage.save(host)
        storage.close()
    if args.json:
        print(json.dumps([r.model_dump(mode="json") for r in results], indent=2))
    else:
        print_table(results)
    return 0 if all(r.ok for r in results) else 1


def _serve(settings: Settings, args: argparse.Namespace) -> int:
    import uvicorn

    from .api import create_app
    from .service import Monitor

    logging.getLogger().setLevel(logging.DEBUG if args.verbose else getattr(logging, settings.log_level.upper(), logging.INFO))
    if not args.verbose:
        logging.getLogger("asyncssh").setLevel(logging.WARNING)  # it logs every connection at INFO
    keys = KeyStore(settings.data_dir)
    keys.public_key()  # generate the key on first start, so the setup page can show it right away
    monitor = Monitor(settings, Storage(settings.data_dir / "history.db"), keys)
    uvicorn.run(create_app(settings, monitor), host=args.bind, port=args.port, log_level="warning", proxy_headers=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vmware-disk-health", description="SMART and SSD health for ESXi hosts")
    parser.add_argument("-c", "--config", type=Path, help="config.yaml or options.json")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="poll the hosts once and print the result")
    collect.add_argument("--host", help="only this host (by name)")
    collect.add_argument("--json", action="store_true", help="print JSON instead of a table")
    collect.add_argument("--no-store", action="store_true", help="do not write to the history database")

    sub.add_parser("pubkey", help="print the SSH public key to authorize on the ESXi hosts")

    serve = sub.add_parser("serve", help="run the scheduler, API and web UI")
    serve.add_argument("--bind", default="0.0.0.0", help="address to listen on (default: all)")
    serve.add_argument("--port", type=int, default=8080)

    forget = sub.add_parser("forget-host-key", help="drop a pinned host key, e.g. after reinstalling ESXi")
    forget.add_argument("host", help="host name from the configuration")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings(args.config)

    if args.command == "pubkey":
        key = KeyStore(settings.data_dir).public_key()
        print(key)
        print(
            f"\nOn each ESXi host (ESXi Shell or SSH as root), run:\n  echo '{key}' >> /etc/ssh/keys-root/authorized_keys",
            file=sys.stderr,
        )
        return 0
    if args.command == "forget-host-key":
        host = next((h for h in settings.hosts if h.name == args.host), None)
        if not host:
            print(f"no host named {args.host!r} in the configuration", file=sys.stderr)
            return 2
        removed = KeyStore(settings.data_dir).forget(host_id(host))
        print("forgotten" if removed else "no pinned key for this host")
        return 0
    if args.command == "serve":
        return _serve(settings, args)
    return asyncio.run(_collect(settings, args))


if __name__ == "__main__":
    raise SystemExit(main())
