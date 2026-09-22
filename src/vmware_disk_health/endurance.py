"""Estimated SSD wear from data written and the vendor's rated endurance (TBW).

Only a fallback for drives that report no usable wear value to ESXi -- for
example the Intel/Solidigm DC SATA drives whose wear attribute esxcli shows
stuck at 100, or a Crucial MX500 without smartctl. The result is always
labelled as an estimate:

* TBW ratings are warranty figures for a standardized (JESD218/219) workload.
  A drive's real NAND wear depends on its write amplification, so the drive
  may be far less (or more) worn than bytes written / TBW suggests.
* esxcli truncates model names to 16 characters, which can cut off the
  generation suffix (``INTEL SSDSC2BA40`` is an S3700 *or* an S3710). The
  lowest matching rating is used then, so the estimate errs towards "more worn".

The table is deliberately small and needs occasional upkeep. Ratings are per
nominal capacity in decimal GB, from the vendors' product specifications.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .model import DiskKind, EnduranceRating

TB = 10**12
# A disk matches a nominal capacity if its size is within this fraction of it
# (a "400 GB" drive reports 400.09 GB, a "1.6 TB" one 1600.3 GB).
CAPACITY_TOLERANCE = 0.08


@dataclass(frozen=True)
class Family:
    name: str
    # Matched against the model with any "INTEL " prefix removed, from the start.
    pattern: str
    # Nominal capacity in GB -> rated endurance in TB written.
    tbw: dict[int, float]


FAMILIES: tuple[Family, ...] = (
    # Intel/Solidigm DC SATA. Model code: SSDSC2 + series + capacity + generation.
    Family("Intel DC S3700", r"SSDSC2BA\d{3}G3", {100: 1825, 200: 3650, 400: 7300, 800: 14600}),
    Family("Intel DC S3710", r"SSDSC2BA\d{3}[GT]4", {200: 3600, 400: 8300, 800: 16900, 1200: 24300}),
    Family("Intel DC S3500", r"SSDSC2BB\d{3}G4", {80: 45, 120: 70, 160: 100, 240: 140, 300: 170, 480: 275, 600: 330, 800: 450}),
    Family("Intel DC S3510", r"SSDSC2BB\d{3}[GT]6", {80: 45, 120: 70, 240: 140, 480: 275, 800: 450, 1200: 660, 1600: 880}),
    Family("Intel DC S3520", r"SSDSC2BB\d{3}[GT]7", {240: 599, 480: 945, 800: 1663, 960: 1750, 1200: 2455, 1600: 2925}),
    # Consumer
    Family("Crucial MX500", r"CT\d+MX500", {250: 100, 500: 180, 1000: 360, 2000: 700, 4000: 1000}),
)

# Model prefixes that esxcli's 16-character truncation leaves ambiguous, and
# the families they may belong to.
TRUNCATED: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("SSDSC2BA", ("Intel DC S3700", "Intel DC S3710")),
    ("SSDSC2BB", ("Intel DC S3500", "Intel DC S3510", "Intel DC S3520")),
)

_BY_NAME = {family.name: family for family in FAMILIES}


def _normalize(model: str) -> str:
    return re.sub(r"^INTEL[\s_]+", "", model.strip().upper())


def _nominal(tbw: dict[int, float], size_bytes: int) -> float | None:
    size_gb = size_bytes / 10**9
    best = min(tbw, key=lambda nominal: abs(nominal - size_gb))
    return tbw[best] if abs(best - size_gb) <= best * CAPACITY_TOLERANCE else None


def rating(model: str, size_bytes: int | None, kind: DiskKind) -> EnduranceRating | None:
    """The rated endurance of a drive, or None if it is not in the table."""
    if kind is DiskKind.HDD or not model or not size_bytes:
        return None
    name = _normalize(model)
    for family in FAMILIES:
        if re.match(family.pattern, name) and (tb := _nominal(family.tbw, size_bytes)):
            return EnduranceRating(family=family.name, tbw_bytes=int(tb * TB))
    for prefix, names in TRUNCATED:
        # Only a name cut off before the generation suffix is ambiguous; a
        # full code that matched no family is a drive we do not know.
        if not re.fullmatch(prefix + r"\d{0,3}", name):
            continue
        candidates = [(tb, n) for n in names if (tb := _nominal(_BY_NAME[n].tbw, size_bytes))]
        if candidates:
            tb, _ = min(candidates)
            return EnduranceRating(family=" / ".join(n for _, n in sorted(candidates)), tbw_bytes=int(tb * TB), ambiguous=True)
    return None


def estimated_life_used(written_bytes: int | None, endurance: EnduranceRating | None) -> float | None:
    """Percent of the rated endurance written so far; may exceed 100."""
    if written_bytes is None or endurance is None or endurance.tbw_bytes <= 0:
        return None
    return round(written_bytes / endurance.tbw_bytes * 100, 1)
