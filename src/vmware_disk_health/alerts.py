"""Notifications when a disk changes status, for the standalone deployment.

Inside Home Assistant the MQTT entities are the better trigger; these channels
are for people running the container on its own.
"""

from __future__ import annotations

import asyncio
import json
import logging
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage

from .config import AlertsConfig
from .model import HostResult, Severity
from .storage import StatusChange

log = logging.getLogger(__name__)

TIMEOUT = 15
PRIORITY = {Severity.CRITICAL: ("urgent", 8), Severity.WARNING: ("high", 6), Severity.OK: ("default", 3)}


def format_change(change: StatusChange) -> tuple[str, str]:
    disk = change.disk
    name = disk.info.model or disk.info.device_id
    where = f"{disk.host} · {disk.info.serial}" if disk.info.serial else disk.host
    if change.kind == "missing":
        return f"Missing: {name}", f"{where}\nThe host no longer reports this disk ({disk.info.device_id})."
    if change.kind == "returned":
        return f"Back: {name}", f"{where}\nThe host reports this disk again. Status: {change.new.label}."
    if change.new is Severity.OK:
        return f"Recovered: {name}", f"{where}\nBack to OK."
    details = "\n".join(f"- {f.message}" for f in disk.findings) or change.new.label
    return f"{change.new.label.capitalize()}: {name}", f"{where}\n{details}"


def should_notify(change: StatusChange, config: AlertsConfig) -> bool:
    # A vanished disk is as serious as a warning: it may have died.
    if change.kind == "missing":
        return config.severity_floor <= Severity.WARNING
    if change.kind == "returned":
        return config.notify_recovery and config.severity_floor <= Severity.WARNING
    if change.new >= config.severity_floor:
        return True
    # A recovery is only interesting if the disk was actually alerted on before.
    return bool(change.new is Severity.OK and config.notify_recovery and change.old and change.old >= config.severity_floor)


class Notifier:
    def __init__(self, config: AlertsConfig):
        self.config = config
        self._unreachable: set[str] = set()

    @property
    def configured(self) -> bool:
        c = self.config
        return bool(c.enabled and (c.ntfy_url or c.gotify_url or (c.smtp_host and c.smtp_to)))

    async def notify(self, title: str, body: str, severity: Severity) -> None:
        await asyncio.gather(
            *(asyncio.to_thread(send, self.config, title, body, severity) for send in (_send_ntfy, _send_gotify, _send_mail)),
            return_exceptions=True,
        )

    async def on_results(self, host: HostResult, changes: list[StatusChange]) -> None:
        """Listener for the monitor."""
        if not self.configured:
            return
        for change in changes:
            if should_notify(change, self.config):
                title, body = format_change(change)
                await self.notify(title, body, change.new)
        if not self.config.notify_host_unreachable:
            return
        if not host.ok and host.name not in self._unreachable:
            self._unreachable.add(host.name)
            await self.notify(f"Host unreachable: {host.name}", f"{host.address}\n{host.error}", Severity.WARNING)
        elif host.ok and host.name in self._unreachable:
            self._unreachable.discard(host.name)
            await self.notify(f"Host reachable again: {host.name}", host.address, Severity.OK)


def _post(url: str, data: bytes, headers: dict[str, str]) -> None:
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310 - user-configured URL
            response.read()
    except (urllib.error.URLError, OSError) as exc:
        log.warning("notification to %s failed: %s", url.split("?")[0], exc)


def _send_ntfy(config: AlertsConfig, title: str, body: str, severity: Severity) -> None:
    if not config.ntfy_url:
        return
    headers = {"Title": title, "Priority": PRIORITY[severity][0], "Tags": "floppy_disk"}
    if config.ntfy_token:
        headers["Authorization"] = f"Bearer {config.ntfy_token}"
    _post(config.ntfy_url, body.encode(), headers)


def _send_gotify(config: AlertsConfig, title: str, body: str, severity: Severity) -> None:
    if not config.gotify_url or not config.gotify_token:
        return
    # The token goes in a header, not the URL, where proxies and logs would keep it.
    url = f"{config.gotify_url.rstrip('/')}/message"
    payload = {"title": title, "message": body, "priority": PRIORITY[severity][1]}
    _post(url, json.dumps(payload).encode(), {"Content-Type": "application/json", "X-Gotify-Key": config.gotify_token})


def _send_mail(config: AlertsConfig, title: str, body: str, _severity: Severity) -> None:
    if not config.smtp_host or not config.smtp_to:
        return
    message = EmailMessage()
    message["Subject"] = f"[VMware Disk Health] {title}"
    message["From"] = config.smtp_from or config.smtp_username or "vmware-disk-health@localhost"
    message["To"] = config.smtp_to
    message.set_content(body)
    try:
        # Port 465 is implicit TLS (SMTPS); anything else upgrades with STARTTLS.
        implicit_tls = config.smtp_tls and config.smtp_port == 465
        smtp_class = smtplib.SMTP_SSL if implicit_tls else smtplib.SMTP
        with smtp_class(config.smtp_host, config.smtp_port, timeout=TIMEOUT) as smtp:
            if config.smtp_tls and not implicit_tls:
                smtp.starttls()
            if config.smtp_username:
                smtp.login(config.smtp_username, config.smtp_password)
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        log.warning("email notification failed: %s", exc)
