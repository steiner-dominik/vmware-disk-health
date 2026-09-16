"""Turn a reading into findings and an overall status.

Finding codes are stable identifiers the UI translates; ``message`` is the
English fallback used in logs, events and notifications.
"""

from __future__ import annotations

from .config import Thresholds
from .model import DiskKind, DiskResult, Finding, Reading, Severity

# Counters are compared against a baseline this old, so "increasing" stays
# set for a while instead of flapping back after the next poll.
RISING_WINDOW_DAYS = 7


def _increased(current: int | None, baseline: int | None) -> bool:
    return current is not None and baseline is not None and current > baseline


def temperature_limits(result: DiskResult, thresholds: Thresholds) -> tuple[float, float]:
    """Effective warning and critical temperature, lowered to the drive's own limit if enabled."""
    warn, crit = thresholds.temperature_limits(result.info.kind)
    limit = result.reading.temperature_limit_c
    if thresholds.use_drive_temp_limit and limit and limit > 0:
        crit = min(crit, limit)
        warn = min(warn, crit - 5)
    return warn, crit


def evaluate(
    result: DiskResult, thresholds: Thresholds, baseline: Reading | None = None, previous: Reading | None = None
) -> DiskResult:
    """Evaluate ``result`` in place.

    ``baseline`` is the reading from about a week ago, which counters are
    compared against; ``previous`` is the one from the last poll, which decides
    whether a pending sector is a blip or is really there.
    """
    r = result.reading
    findings: list[Finding] = []

    def add(code: str, severity: Severity, message: str, value: float | str | None = None, limit: float | None = None):
        findings.append(Finding(code=code, severity=severity, message=message, value=value, limit=limit))

    if not result.sources:
        add("no_data", Severity.UNKNOWN, "; ".join(result.errors) or "no health data available")
        result.findings, result.status = findings, Severity.UNKNOWN
        return result

    if r.health_passed is False:
        add("health_failed", Severity.CRITICAL, "drive reports failing health")
    for name in r.failing_attributes:
        add("attribute_failing", Severity.CRITICAL, f"SMART attribute {name} is below its failure threshold", name)
    for flag in r.critical_warnings:
        add(f"nvme_{flag}", Severity.CRITICAL, f"NVMe critical warning: {flag.replace('_', ' ')}")
    spare, spare_min = r.available_spare_pct, r.available_spare_threshold_pct
    if spare is not None and spare_min and spare <= spare_min:
        add("spare_low", Severity.CRITICAL, "available spare at or below threshold", spare, spare_min)

    remaining = r.life_remaining_pct
    if result.info.kind is not DiskKind.HDD and remaining is not None:
        message = f"{remaining:.0f}% endurance remaining"
        if remaining <= thresholds.life_remaining_crit_pct:
            add("life_low", Severity.CRITICAL, message, remaining, thresholds.life_remaining_crit_pct)
        elif remaining <= thresholds.life_remaining_warn_pct:
            add("life_low", Severity.WARNING, message, remaining, thresholds.life_remaining_warn_pct)

    base = baseline or Reading()
    if r.pending_sectors:
        n = r.pending_sectors
        # Drives report a pending sector and clear it again once the sector is
        # re-read successfully, so one sighting is a warning and only a count
        # that is still there at the next poll is critical.
        if previous is not None and previous.pending_sectors:
            add("pending_sectors_persisting", Severity.CRITICAL, f"{n} pending sectors, still there at the next poll", n)
        else:
            add("pending_sectors", Severity.WARNING, f"{n} pending sectors", n)
    if r.offline_uncorrectable:
        n = r.offline_uncorrectable
        add("offline_uncorrectable", Severity.CRITICAL, f"{n} offline uncorrectable sectors", n)
    if r.reallocated_sectors:
        n = r.reallocated_sectors
        if _increased(n, base.reallocated_sectors):
            add("reallocated_sectors_rising", Severity.CRITICAL, f"reallocated sectors increased to {n}", n)
        else:
            add("reallocated_sectors", Severity.WARNING, f"{n} reallocated sectors", n)
    if r.reported_uncorrectable:
        n = r.reported_uncorrectable
        add("reported_uncorrectable", Severity.WARNING, f"{n} reported uncorrectable errors", n)
    if _increased(r.crc_errors, base.crc_errors):
        n = r.crc_errors
        add("crc_errors_rising", Severity.WARNING, f"interface CRC errors increased to {n} (check cable/backplane)", n)
    if r.media_errors:
        n = r.media_errors
        if _increased(n, base.media_errors):
            add("media_errors_rising", Severity.CRITICAL, f"media errors increased to {n}", n)
        else:
            add("media_errors", Severity.WARNING, f"{n} media errors", n)

    if r.temperature_c is not None:
        warn, crit = temperature_limits(result, thresholds)
        t = r.temperature_c
        if t >= crit:
            add("temperature", Severity.CRITICAL, f"temperature {t:.0f} °C (limit {crit:.0f} °C)", t, crit)
        elif t >= warn:
            add("temperature", Severity.WARNING, f"temperature {t:.0f} °C (warning at {warn:.0f} °C)", t, warn)

    result.findings = findings
    result.status = max((f.severity for f in findings), default=Severity.OK)
    return result
