"""Turn a reading into findings and an overall status."""

from __future__ import annotations

from .config import Thresholds
from .model import DiskKind, DiskResult, Finding, Reading, Severity


def _increased(current: int | None, previous: int | None) -> bool:
    return current is not None and previous is not None and current > previous


def evaluate(result: DiskResult, thresholds: Thresholds, previous: Reading | None = None) -> DiskResult:
    r = result.reading
    kind = result.info.kind
    findings: list[Finding] = []

    def add(code: str, severity: Severity, message: str, value: float | int | str | None = None) -> None:
        findings.append(Finding(code=code, severity=severity, message=message, value=value))

    if not result.sources:
        add("no_data", Severity.UNKNOWN, "; ".join(result.errors) or "no health data available")
        result.findings, result.status = findings, Severity.UNKNOWN
        return result

    if r.health_passed is False:
        add("health_failed", Severity.CRITICAL, "drive reports failing health")
    for flag in r.critical_warnings:
        add(f"nvme_{flag}", Severity.CRITICAL, f"NVMe critical warning: {flag.replace('_', ' ')}")
    if (
        r.available_spare_pct is not None
        and r.available_spare_threshold_pct
        and r.available_spare_pct <= r.available_spare_threshold_pct
    ):
        add("spare_low", Severity.CRITICAL, "available spare at or below threshold", r.available_spare_pct)

    if kind is not DiskKind.HDD and r.life_remaining_pct is not None:
        if r.life_remaining_pct <= thresholds.life_remaining_crit_pct:
            add("life_low", Severity.CRITICAL, f"{r.life_remaining_pct:.0f}% endurance remaining", r.life_remaining_pct)
        elif r.life_remaining_pct <= thresholds.life_remaining_warn_pct:
            add("life_low", Severity.WARNING, f"{r.life_remaining_pct:.0f}% endurance remaining", r.life_remaining_pct)

    prev = previous or Reading()
    if r.pending_sectors:
        add("pending_sectors", Severity.CRITICAL, f"{r.pending_sectors} pending sectors", r.pending_sectors)
    if r.offline_uncorrectable:
        add(
            "offline_uncorrectable",
            Severity.CRITICAL,
            f"{r.offline_uncorrectable} offline uncorrectable sectors",
            r.offline_uncorrectable,
        )
    if r.reallocated_sectors:
        rising = _increased(r.reallocated_sectors, prev.reallocated_sectors)
        add(
            "reallocated_sectors",
            Severity.CRITICAL if rising else Severity.WARNING,
            f"{r.reallocated_sectors} reallocated sectors" + (" (increasing)" if rising else ""),
            r.reallocated_sectors,
        )
    if r.reported_uncorrectable:
        add(
            "reported_uncorrectable",
            Severity.WARNING,
            f"{r.reported_uncorrectable} reported uncorrectable errors",
            r.reported_uncorrectable,
        )
    if _increased(r.crc_errors, prev.crc_errors):
        add(
            "crc_errors_rising",
            Severity.WARNING,
            f"interface CRC errors increased to {r.crc_errors} (check cable/backplane)",
            r.crc_errors,
        )
    if r.media_errors:
        rising = _increased(r.media_errors, prev.media_errors)
        add(
            "media_errors",
            Severity.CRITICAL if rising else Severity.WARNING,
            f"{r.media_errors} media errors" + (" (increasing)" if rising else ""),
            r.media_errors,
        )

    if r.temperature_c is not None:
        warn, crit = thresholds.temperature_limits(kind)
        if thresholds.use_drive_temp_limit and r.temperature_limit_c and r.temperature_limit_c > 0:
            crit = min(crit, r.temperature_limit_c)
            warn = min(warn, crit - 5)
        if r.temperature_c >= crit:
            add("temperature", Severity.CRITICAL, f"temperature {r.temperature_c:.0f} °C (limit {crit:.0f} °C)", r.temperature_c)
        elif r.temperature_c >= warn:
            add(
                "temperature",
                Severity.WARNING,
                f"temperature {r.temperature_c:.0f} °C (warning at {warn:.0f} °C)",
                r.temperature_c,
            )

    result.findings = findings
    result.status = max((f.severity for f in findings), default=Severity.OK)
    return result
