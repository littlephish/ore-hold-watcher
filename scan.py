# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LittlePhish
"""Parser for EVE survey-scanner results pasted from the game client.

The scan window copies as tab-separated rows:

    Coesite\t18,611\t186,110 m3\t3,230,000.00 ISK\t726 m

Pure functions only - no Qt, no engine state, no I/O - so the whole thing is
testable headlessly. Rows whose volume disagrees with units x unit_volume are
dropped rather than trusted: a mispaste must never become a confident wrong
countdown.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Volume tolerance: EVE rounds the m3 column, so allow a small relative drift.
VOLUME_TOLERANCE = 0.005   # 0.5%


@dataclass(frozen=True)
class ScanRow:
    ore: str
    units: int
    m3: float
    isk: float
    distance_m: float


# Leading numeric token: "190,680 m3" -> "190,680", "1.00 ISK" -> "1.00".
# Must NOT simply strip non-digits: the "3" in the "m3" unit suffix would be
# scraped into the number (190,680 m3 -> 1906803).
_NUM_RE = re.compile(r"\d[\d,\s']*(?:\.\d+)?")


def _num(raw: str) -> float:
    """'1,244.50' / '1 244' / '190,680 m3' -> float. Returns 0.0 when absent."""
    m = _NUM_RE.search(raw or "")
    if not m:
        return 0.0
    cleaned = re.sub(r"[,\s']", "", m.group(0))
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _distance_m(raw: str) -> float:
    """'726 m' -> 726.0, '92 km' -> 92000.0."""
    value = _num(raw)
    return value * 1000.0 if "km" in raw.lower() else value


def parse_scan(text: str, table) -> tuple[list[ScanRow], list[str]]:
    """Parse a pasted survey scan.

    `table` is an engine.OreTable. Returns (rows, warnings); never raises.
    """
    rows: list[ScanRow] = []
    warnings: list[str] = []
    if not text or not text.strip():
        return rows, warnings

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) < 5:
            warnings.append(f"Skipped unparseable line: {line[:60]!r}")
            continue

        ore = parts[0]
        units = int(_num(parts[1]))
        m3 = _num(parts[2])
        isk = _num(parts[3])
        distance_m = _distance_m(parts[4])

        if units <= 0:
            warnings.append(f"Skipped {ore!r}: no quantity")
            continue

        unit_vol = table.unit_volume(ore)
        if unit_vol is None:
            warnings.append(
                f"Skipped {ore!r}: unknown ore - add it to ores_override.json")
            continue

        expected = units * unit_vol
        if expected > 0 and abs(m3 - expected) / expected > VOLUME_TOLERANCE:
            warnings.append(
                f"Skipped {ore!r}: {m3:,.0f} m3 does not match "
                f"{units:,} units x {unit_vol} m3 ({expected:,.0f} m3)")
            continue

        rows.append(ScanRow(ore=ore, units=units, m3=m3, isk=isk,
                            distance_m=distance_m))

    return rows, warnings
