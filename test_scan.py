# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LittlePhish
"""Headless tests for the survey-scan parser. Run: python test_scan.py"""
from engine import OreTable
from scan import ScanRow, parse_scan

# Verbatim rows from a real survey scanner paste (tab-separated).
SAMPLE = "\n".join([
    "Bitumens\t19,068\t190,680 m3\t7,380,000.00 ISK\t92 km",
    "Bitumens\t4,395\t43,950 m3\t1,700,000.00 ISK\t37 km",
    "Brimful Coesite\t267\t2,670 m3\t59,000.00 ISK\t67 km",
    "Brimful Sylvite\t17,974\t179,740 m3\t5,750,000.00 ISK\t32 km",
    "Coesite\t18,611\t186,110 m3\t3,230,000.00 ISK\t726 m",
    "Coesite\t19,096\t190,960 m3\t3,310,000.00 ISK\t100 km",
])


def approx(a, b, tol=0.01):
    assert abs(a - b) < tol, f"{a} != {b}"


def main():
    table = OreTable()

    rows, warnings = parse_scan(SAMPLE, table)
    assert len(rows) == 6, f"expected 6 rows, got {len(rows)}"
    assert not warnings, f"unexpected warnings: {warnings}"

    # metres vs kilometres normalisation (spec F5: 726 m is the locked rock)
    near = [r for r in rows if r.distance_m < 1000]
    assert len(near) == 1
    assert near[0].ore == "Coesite" and near[0].units == 18611
    approx(near[0].distance_m, 726.0)

    # thousands separators parsed
    assert rows[0].units == 19068
    approx(rows[0].m3, 190680.0)
    approx(rows[0].isk, 7380000.0)

    # km normalised to metres
    approx(rows[-1].distance_m, 100000.0)

    # spec F4: Brimful/Glistening resolve via the existing suffix rule
    brim = [r for r in rows if r.ore == "Brimful Coesite"][0]
    approx(brim.m3, 267 * 10.0)

    # a row whose volume contradicts units x unit_volume is dropped, not trusted
    bad = "Coesite\t100\t999,999 m3\t1.00 ISK\t10 km"
    rows_b, warn_b = parse_scan(bad, table)
    assert rows_b == [], "volume mismatch must be rejected"
    assert any("Coesite" in w for w in warn_b), warn_b

    # unknown ore is dropped and named
    rows_u, warn_u = parse_scan("Unobtanium\t10\t100 m3\t1.00 ISK\t5 km", table)
    assert rows_u == []
    assert any("Unobtanium" in w for w in warn_u), warn_u

    # empty and junk input never raise
    assert parse_scan("", table) == ([], [])
    rows_j, warn_j = parse_scan("not a scan at all", table)
    assert rows_j == [] and warn_j

    # blank lines and trailing whitespace tolerated
    rows_w, _ = parse_scan("\n  \n" + SAMPLE + "\n\n", table)
    assert len(rows_w) == 6

    # ScanRow is hashable/frozen so dialog code can stash it as item data
    assert isinstance(rows[0], ScanRow)

    print("test_scan: OK")


if __name__ == "__main__":
    main()
