"""SQLite history: latest state per disk, hourly samples, daily rollups, status events."""

from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .model import NUMERIC_FIELDS, DiskResult, HostResult, Reading, Severity

SCHEMA_VERSION = 1
FLOAT_FIELDS = {"temperature_c", "life_used_pct", "available_spare_pct"}

_SAMPLE_COLUMNS = ", ".join(f"{field} REAL" for field in NUMERIC_FIELDS)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS hosts (
    name TEXT PRIMARY KEY,
    address TEXT NOT NULL,
    last_attempt REAL,
    last_success REAL,
    last_error TEXT,
    esxi_version TEXT,
    smartctl_path TEXT,
    duration_s REAL,
    esxcli_json INTEGER
);
CREATE TABLE IF NOT EXISTS disks (
    key TEXT PRIMARY KEY,
    host TEXT NOT NULL,
    status INTEGER NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    latest_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS samples (
    disk_key TEXT NOT NULL,
    ts REAL NOT NULL,
    status INTEGER NOT NULL,
    {_SAMPLE_COLUMNS},
    PRIMARY KEY (disk_key, ts)
);
CREATE TABLE IF NOT EXISTS daily (
    disk_key TEXT NOT NULL,
    day TEXT NOT NULL,
    ts REAL NOT NULL,
    status INTEGER NOT NULL,
    temperature_min REAL,
    temperature_max REAL,
    {_SAMPLE_COLUMNS},
    PRIMARY KEY (disk_key, day)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    disk_key TEXT,
    host TEXT NOT NULL,
    old_status INTEGER,
    new_status INTEGER NOT NULL,
    summary TEXT NOT NULL,
    findings_json TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
"""


@dataclass
class StatusChange:
    disk: DiskResult
    old: Severity | None
    new: Severity


class Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(hosts)")}
        if "esxcli_json" not in columns:  # databases created by 0.1.0 during development
            self.db.execute("ALTER TABLE hosts ADD COLUMN esxcli_json INTEGER")
        if "findings_json" not in {row["name"] for row in self.db.execute("PRAGMA table_info(events)")}:
            self.db.execute("ALTER TABLE events ADD COLUMN findings_json TEXT")
        self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def previous_reading(self, disk_key: str) -> Reading | None:
        row = self.db.execute("SELECT latest_json FROM disks WHERE key = ?", (disk_key,)).fetchone()
        return DiskResult.model_validate_json(row["latest_json"]).reading if row else None

    def baseline_reading(self, disk_key: str, since: float) -> Reading | None:
        """The oldest sample at or after ``since``: what "increasing" is measured against.

        Falls back to the latest state for disks without samples in the window.
        """
        row = self.db.execute(
            f"SELECT {', '.join(NUMERIC_FIELDS)} FROM samples WHERE disk_key = ? AND ts >= ? ORDER BY ts LIMIT 1",
            (disk_key, since),
        ).fetchone()
        if row is None:
            return self.previous_reading(disk_key)
        return _reading_from_row(row)

    def disk(self, disk_key: str) -> DiskResult | None:
        row = self.db.execute("SELECT latest_json FROM disks WHERE key = ?", (disk_key,)).fetchone()
        return DiskResult.model_validate_json(row["latest_json"]) if row else None

    def save(self, host: HostResult) -> list[StatusChange]:
        """Persist one host's collection run and return disks whose status changed."""
        now = host.collected_at or time.time()
        changes: list[StatusChange] = []
        with self.db:
            self.db.execute(
                """INSERT INTO hosts (name, address, last_attempt, last_success, last_error, esxi_version, smartctl_path,
                                      duration_s, esxcli_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     address = excluded.address,
                     last_attempt = excluded.last_attempt,
                     last_success = COALESCE(excluded.last_success, hosts.last_success),
                     last_error = excluded.last_error,
                     esxi_version = COALESCE(excluded.esxi_version, hosts.esxi_version),
                     smartctl_path = CASE WHEN excluded.last_success IS NULL THEN hosts.smartctl_path ELSE excluded.smartctl_path END,
                     duration_s = excluded.duration_s,
                     esxcli_json = CASE WHEN excluded.last_success IS NULL THEN hosts.esxcli_json ELSE excluded.esxcli_json END""",
                (
                    host.name,
                    host.address,
                    now,
                    now if host.ok else None,
                    host.error,
                    host.esxi_version,
                    host.smartctl_path,
                    host.duration_s,
                    int(host.esxcli_json),
                ),
            )
            if not host.ok:
                return changes
            for disk in host.disks:
                row = self.db.execute("SELECT status FROM disks WHERE key = ?", (disk.key,)).fetchone()
                old = Severity(row["status"]) if row else None
                self.db.execute(
                    """INSERT INTO disks (key, host, status, first_seen, last_seen, latest_json) VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET host = excluded.host, status = excluded.status,
                         last_seen = excluded.last_seen, latest_json = excluded.latest_json""",
                    (disk.key, host.name, int(disk.status), now, now, disk.model_dump_json()),
                )
                values = [_value(disk.reading, field) for field in NUMERIC_FIELDS]
                self.db.execute(
                    f"INSERT OR REPLACE INTO samples (disk_key, ts, status, {', '.join(NUMERIC_FIELDS)}) "
                    f"VALUES (?, ?, ?, {', '.join('?' * len(NUMERIC_FIELDS))})",
                    (disk.key, now, int(disk.status), *values),
                )
                # A newly discovered healthy disk is not news; anything else is.
                if old != disk.status and not (old is None and disk.status is Severity.OK):
                    summary = "; ".join(f.message for f in disk.findings) or disk.status.label
                    findings = json.dumps([f.model_dump(mode="json") for f in disk.findings])
                    self.db.execute(
                        "INSERT INTO events (ts, disk_key, host, old_status, new_status, summary, findings_json)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (now, disk.key, host.name, None if old is None else int(old), int(disk.status), summary, findings),
                    )
                    changes.append(StatusChange(disk, old, disk.status))
        return changes

    def hosts(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM hosts ORDER BY name")]

    def disks(self) -> list[DiskResult]:
        rows = self.db.execute("SELECT latest_json FROM disks ORDER BY host, key")
        return [DiskResult.model_validate_json(row["latest_json"]) for row in rows]

    def last_seen(self) -> dict[str, float]:
        return {row["key"]: row["last_seen"] for row in self.db.execute("SELECT key, last_seen FROM disks")}

    def history(self, disk_key: str, since: float = 0) -> list[dict]:
        """Daily rollups followed by raw samples, oldest first."""
        fields = ", ".join(NUMERIC_FIELDS)
        rows = self.db.execute(
            f"""SELECT ts, 1 AS daily, status, temperature_min, temperature_max, {fields} FROM daily WHERE disk_key = ? AND ts >= ?
                UNION ALL
                SELECT ts, 0 AS daily, status, NULL, NULL, {fields} FROM samples WHERE disk_key = ? AND ts >= ?
                ORDER BY ts""",
            (disk_key, since, disk_key, since),
        )
        return [dict(row) for row in rows]

    def events(self, limit: int = 100) -> list[dict]:
        rows = [dict(row) for row in self.db.execute("SELECT * FROM events ORDER BY ts DESC, id DESC LIMIT ?", (limit,))]
        for row in rows:
            row["findings"] = json.loads(row.pop("findings_json") or "[]")
        return rows

    def rollup(self, retention_days: int, now: float | None = None) -> int:
        """Fold raw samples older than the retention into one row per disk and day."""
        # Aligned to UTC midnight so a day is never rolled up half at a time.
        cutoff = ((now or time.time()) - retention_days * 86400) // 86400 * 86400
        rows = self.db.execute("SELECT * FROM samples WHERE ts < ? ORDER BY disk_key, ts", (cutoff,)).fetchall()
        if not rows:
            return 0
        groups: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            day = datetime.fromtimestamp(row["ts"], UTC).strftime("%Y-%m-%d")
            groups[(row["disk_key"], day)].append(row)
        with self.db:
            for (disk_key, day), group in groups.items():
                last = group[-1]
                temps = [r["temperature_c"] for r in group if r["temperature_c"] is not None]
                values = {field: last[field] for field in NUMERIC_FIELDS}
                if temps:
                    values["temperature_c"] = round(sum(temps) / len(temps), 1)
                self.db.execute(
                    f"INSERT OR REPLACE INTO daily (disk_key, day, ts, status, temperature_min, temperature_max, {', '.join(NUMERIC_FIELDS)}) "
                    f"VALUES (?, ?, ?, ?, ?, ?, {', '.join('?' * len(NUMERIC_FIELDS))})",
                    (
                        disk_key,
                        day,
                        last["ts"],
                        max(r["status"] for r in group),
                        min(temps) if temps else None,
                        max(temps) if temps else None,
                        *values.values(),
                    ),
                )
            self.db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        return len(rows)


def _reading_from_row(row: sqlite3.Row) -> Reading:
    """Samples are stored as REAL; counters go back to integers."""
    values = {}
    for field in NUMERIC_FIELDS:
        value = row[field]
        if value is not None:
            values[field] = value if field in FLOAT_FIELDS else round(value)
    return Reading(**values)


def _value(reading: Reading, field: str) -> float | None:
    value = getattr(reading, field)
    return None if value is None else float(value)
