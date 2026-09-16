"""HTTP API, Prometheus metrics and the web UI."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, insights, metrics
from .config import Settings
from .evaluate import temperature_limits
from .model import DiskResult, Severity
from .service import Integrations, Monitor
from .transport import host_id

WEB_DIR = Path(__file__).parent / "web"
DAY = 86400


class RevalidatingStaticFiles(StaticFiles):
    """Browsers must check for a newer UI after an update; ETags keep that to a cheap 304."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _summary(disk: DiskResult, last_seen: float, host_success: float | None, name: str | None = None) -> dict:
    r = disk.reading
    return {
        "key": disk.key,
        "name": name,
        "host": disk.host,
        "status": disk.status.label,
        "model": disk.info.model,
        "serial": disk.info.serial,
        "kind": disk.info.kind.value,
        "size_bytes": disk.info.size_bytes,
        "is_boot": disk.info.is_boot,
        "temperature_c": r.temperature_c,
        "life_remaining_pct": r.life_remaining_pct,
        "written_bytes": r.written_bytes,
        "power_on_hours": r.power_on_hours,
        "sources": disk.sources,
        "findings": [f.model_dump(mode="json") for f in disk.findings],
        "last_seen": last_seen,
        # Seen before, but not in the host's latest successful collection.
        "missing": bool(host_success and last_seen < host_success),
    }


def create_app(
    settings: Settings, monitor: Monitor, *, start_scheduler: bool = True, integrations: Integrations | None = None
) -> FastAPI:
    storage = monitor.storage

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if integrations:
            await integrations.start()
        if start_scheduler:
            monitor.start()
        yield
        await monitor.stop()
        if integrations:
            await integrations.stop()

    app = FastAPI(title="VMware Disk Health", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)

    def display_name(disk: DiskResult) -> str | None:
        override = settings.override_for(disk.info)
        return override.name if override else None

    def host_config(name: str):
        host = next((h for h in settings.hosts if h.name == name), None)
        if host is None:
            raise HTTPException(404, f"no host named {name!r}")
        return host

    @app.get("/healthz")
    def healthz():
        if not monitor.healthy:
            return JSONResponse({"ok": False, "reason": "scheduler stopped"}, status_code=503)
        return {"ok": True}

    @app.get("/api/state")
    def state():
        stored_hosts = {h["name"]: h for h in storage.hosts()}
        last_seen = storage.last_seen()
        disks = storage.disks()
        hosts = []
        for config in settings.hosts:
            row = stored_hosts.get(config.name, {})
            hosts.append(
                {
                    "name": config.name,
                    "address": config.address,
                    "enabled": config.enabled,
                    "last_attempt": row.get("last_attempt"),
                    "last_success": row.get("last_success"),
                    "last_error": row.get("last_error"),
                    "esxi_version": row.get("esxi_version"),
                    "smartctl_path": row.get("smartctl_path"),
                    "esxcli_json": bool(row.get("esxcli_json")),
                    "duration_s": row.get("duration_s"),
                }
            )
        configured = {h.name for h in settings.hosts}
        summaries = [
            _summary(d, last_seen.get(d.key, 0), stored_hosts.get(d.host, {}).get("last_success"), display_name(d))
            for d in disks
            if d.host in configured
        ]
        counts = {s.label: 0 for s in Severity}
        for disk in summaries:
            counts["unknown" if disk["missing"] else disk["status"]] += 1
        return {
            "app": {
                "version": __version__,
                "language": settings.language,
                "ha_mode": settings.ha_mode,
                "poll_interval_minutes": settings.poll_interval_minutes,
                "polling": monitor.polling,
                "last_poll_started": monitor.last_poll_started,
                "last_poll_finished": monitor.last_poll_finished,
                "next_poll_at": monitor.next_poll_at,
                "now": time.time(),
            },
            "counts": counts,
            "hosts": hosts,
            "disks": summaries,
        }

    @app.get("/api/disks/{key:path}/history")
    def disk_history(key: str, days: int = Query(default=90, ge=0, le=36500)):
        if storage.disk(key) is None:
            raise HTTPException(404, "unknown disk")
        since = 0 if days == 0 else time.time() - days * DAY
        points = storage.history(key, since)
        return {"points": points, "written_per_day": insights.written_per_day_series(points)}

    @app.get("/api/disks/{key:path}")
    def disk_detail(key: str):
        disk = storage.disk(key)
        if disk is None:
            raise HTTPException(404, "unknown disk")
        now = time.time()
        history = storage.history(key, now - 365 * DAY)
        warn, crit = temperature_limits(disk, settings.thresholds_for(disk.info))
        host = next((h for h in storage.hosts() if h["name"] == disk.host), {})
        last_seen = storage.last_seen().get(key, 0)
        return {
            "key": key,
            "disk": disk.model_dump(mode="json"),
            "host": host,
            "summary": _summary(disk, last_seen, host.get("last_success"), display_name(disk)),
            "insights": insights.compute(disk, history, now).model_dump(),
            "temperature_warn_c": warn,
            "temperature_crit_c": crit,
            "thresholds": settings.thresholds_for(disk.info).model_dump(),
        }

    @app.get("/api/events")
    def events(limit: int = Query(default=200, ge=1, le=2000)):
        return {"events": storage.events(limit)}

    @app.post("/api/poll", status_code=202)
    def poll_now():
        return {"started": monitor.trigger()}

    @app.get("/api/setup")
    def setup():
        key = monitor.keys.public_key()
        return {
            "public_key": key,
            "authorize_command": f"echo '{key}' >> /etc/ssh/keys-root/authorized_keys",
            "ha_mode": settings.ha_mode,
            "integrations": integrations.status() if integrations else {},
            "hosts": [
                {
                    "name": h.name,
                    "address": h.address,
                    "port": h.port,
                    "username": h.username,
                    "auth": "password" if h.password else "key",
                    "smartctl_setting": h.smartctl_path,
                    "exclude": h.exclude,
                    "host_key": monitor.fingerprint(h),
                }
                for h in settings.hosts
            ],
            "thresholds": settings.thresholds.model_dump(),
            "poll_interval_minutes": settings.poll_interval_minutes,
            "retention_raw_days": settings.retention_raw_days,
        }

    @app.post("/api/hosts/{name}/test")
    async def test_host(name: str):
        return await monitor.test_host(host_config(name))

    @app.post("/api/hosts/{name}/forget-host-key")
    def forget_host_key(name: str):
        # Resetting a pinned key is what an attacker on the network would want, so
        # it needs an authenticated UI: Home Assistant ingress. Standalone: use the CLI.
        if not settings.ha_mode:
            raise HTTPException(403, "only available through Home Assistant; use `vmware-disk-health forget-host-key`")
        host = host_config(name)
        return {"forgotten": monitor.keys.forget(host_id(host))}

    @app.get("/metrics", response_class=PlainTextResponse)
    def prometheus():
        configured = {h.name for h in settings.hosts}
        hosts = [h for h in storage.hosts() if h["name"] in configured]
        return metrics.render(hosts, [d for d in storage.disks() if d.host in configured])

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", RevalidatingStaticFiles(directory=WEB_DIR), name="static")
    return app
