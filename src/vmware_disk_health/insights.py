"""Derived values: how fast a disk is written and when its endurance runs out."""

from __future__ import annotations

from pydantic import BaseModel

from .model import DiskKind, DiskResult

DAY = 86400
# Use measured history once it covers this long; before that, lifetime averages.
MIN_HISTORY_DAYS = 7
PROJECTION_CAP_YEARS = 50


class Insights(BaseModel):
    written_per_day_bytes: float | None = None
    written_basis: str | None = None  # "history" or "lifetime"
    life_used_per_year_pct: float | None = None
    life_basis: str | None = None
    life_end_ts: float | None = None  # projected date of 100% endurance used
    life_end_beyond_cap: bool = False


def _span(points: list[dict], field: str, window_days: int, now: float) -> tuple[dict, dict] | None:
    """First and last point with a value inside the window, if they are far enough apart."""
    usable = [p for p in points if p.get(field) is not None and p["ts"] >= now - window_days * DAY]
    if len(usable) < 2 or usable[-1]["ts"] - usable[0]["ts"] < MIN_HISTORY_DAYS * DAY:
        return None
    return usable[0], usable[-1]


def compute(disk: DiskResult, history: list[dict], now: float) -> Insights:
    r = disk.reading
    out = Insights()
    hours = r.power_on_hours

    span = _span(history, "written_bytes", 30, now)
    if span and span[1]["written_bytes"] >= span[0]["written_bytes"]:
        days = (span[1]["ts"] - span[0]["ts"]) / DAY
        out.written_per_day_bytes = (span[1]["written_bytes"] - span[0]["written_bytes"]) / days
        out.written_basis = "history"
    elif r.written_bytes is not None and hours:
        out.written_per_day_bytes = r.written_bytes / hours * 24
        out.written_basis = "lifetime"

    if disk.info.kind is DiskKind.HDD or r.life_used_pct is None:
        return out
    # Percentages move in whole steps, so the window is long and must show an actual change.
    span = _span(history, "life_used_pct", 365, now)
    if span and span[1]["life_used_pct"] > span[0]["life_used_pct"]:
        years = (span[1]["ts"] - span[0]["ts"]) / DAY / 365.25
        out.life_used_per_year_pct = (span[1]["life_used_pct"] - span[0]["life_used_pct"]) / years
        out.life_basis = "history"
    elif r.life_used_pct > 0 and hours:
        out.life_used_per_year_pct = r.life_used_pct / (hours / 24 / 365.25)
        out.life_basis = "lifetime"

    if out.life_used_per_year_pct:
        remaining_years = max(0.0, 100 - r.life_used_pct) / out.life_used_per_year_pct
        if remaining_years > PROJECTION_CAP_YEARS:
            out.life_end_beyond_cap = True
        else:
            out.life_end_ts = now + remaining_years * 365.25 * DAY
    return out


def written_per_day_series(history: list[dict]) -> list[dict]:
    """Bytes written per calendar day (UTC), from consecutive cumulative readings.

    Drops negative steps, which mean a counter reset or a replaced disk.
    """
    per_day: dict[int, float] = {}
    previous = None
    for point in history:
        value = point.get("written_bytes")
        if value is None:
            continue
        if previous is not None and value >= previous["written_bytes"]:
            day = int(point["ts"] // DAY * DAY)
            per_day[day] = per_day.get(day, 0.0) + value - previous["written_bytes"]
        previous = point
    return [{"ts": day, "bytes": total} for day, total in sorted(per_day.items())]
