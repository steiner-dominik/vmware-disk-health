"""Derived values: how fast a disk is written and when its endurance runs out."""

from __future__ import annotations

from pydantic import BaseModel

from .model import DiskKind, DiskResult

DAY = 86400
# Use measured history once it covers this long; before that, lifetime averages.
MIN_HISTORY_DAYS = 7
PROJECTION_CAP_YEARS = 50
# Faster than any SATA/SAS/NVMe drive can sustain: a step this steep is a
# counter that changed meaning (a unit fix, a replaced disk), not real writes.
MAX_PLAUSIBLE_WRITE_BYTES_PER_S = 15 * 10**9


class Insights(BaseModel):
    written_per_day_bytes: float | None = None
    written_basis: str | None = None  # "history" or "lifetime"
    life_used_per_year_pct: float | None = None
    life_basis: str | None = None
    life_end_ts: float | None = None  # projected date of 100% endurance used
    life_end_beyond_cap: bool = False
    # Set when the projection comes from data written against the rated TBW.
    life_estimated: bool = False


def written_steps(history: list[dict]):
    """(timestamp, bytes written since the previous reading) for consecutive readings.

    Drops negative steps (counter reset, replaced disk) and implausibly steep
    ones (a counter whose unit changed), so neither shows up as writes.
    """
    previous = None
    for point in history:
        value = point.get("written_bytes")
        if value is None:
            continue
        if previous is not None:
            delta, seconds = value - previous["written_bytes"], point["ts"] - previous["ts"]
            if delta >= 0 and seconds > 0 and delta / seconds <= MAX_PLAUSIBLE_WRITE_BYTES_PER_S:
                yield point["ts"], delta
        previous = point


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
    if span:
        window = [p for p in history if span[0]["ts"] <= p["ts"] <= span[1]["ts"]]
        days = (span[1]["ts"] - span[0]["ts"]) / DAY
        out.written_per_day_bytes = sum(delta for _, delta in written_steps(window)) / days
        out.written_basis = "history"
    elif r.written_bytes is not None and hours:
        out.written_per_day_bytes = r.written_bytes / hours * 24
        out.written_basis = "lifetime"

    if disk.info.kind is DiskKind.HDD:
        return out
    if r.life_used_pct is None:
        # Only an estimate is possible: remaining rated TBW at the current write rate.
        if disk.endurance and r.written_bytes is not None and out.written_per_day_bytes:
            out.life_estimated = True
            out.life_basis = out.written_basis
            remaining_days = max(0, disk.endurance.tbw_bytes - r.written_bytes) / out.written_per_day_bytes
            if remaining_days / 365.25 > PROJECTION_CAP_YEARS:
                out.life_end_beyond_cap = True
            else:
                out.life_end_ts = now + remaining_days * DAY
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

    See written_steps for what is left out.
    """
    per_day: dict[int, float] = {}
    for ts, delta in written_steps(history):
        day = int(ts // DAY * DAY)
        per_day[day] = per_day.get(day, 0.0) + delta
    return [{"ts": day, "bytes": total} for day, total in sorted(per_day.items())]
