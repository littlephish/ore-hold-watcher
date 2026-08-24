# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LittlePhish
"""Headless tests for SDE catalog generation. Run: python test_sde.py"""
import json
import sqlite3
import tempfile
from pathlib import Path

from engine import OreTable
from sde import build_volume_catalog, write_catalog


def main():
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "sde.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE invCategories (categoryID INTEGER PRIMARY KEY, categoryName TEXT);
        CREATE TABLE invGroups (groupID INTEGER PRIMARY KEY, categoryID INTEGER, groupName TEXT);
        CREATE TABLE invTypes (
            typeID INTEGER PRIMARY KEY, groupID INTEGER, typeName TEXT,
            volume FLOAT, published BOOLEAN
        );
    """)
    con.executemany("INSERT INTO invCategories VALUES (?, ?)", [
        (25, "Asteroid"), (26, "Ice"), (13, "Planetary Resources"),
        (6, "Ships"),
    ])
    con.executemany("INSERT INTO invGroups VALUES (?, ?, ?)", [
        (185, 25, "Asteroid Ore"), (186, 26, "Ice Products"),
        (711, 13, "Harvestable Cloud"), (27, 6, "Shuttle"),
    ])
    con.executemany("INSERT INTO invTypes VALUES (?, ?, ?, ?, ?)", [
        (18, 185, "Plagioclase", 0.35, 1),
        (17456, 185, "Plagioclase III-Grade", 0.35, 1),
        (16274, 186, "Blue Ice", 1000.0, 1),
        (25268, 711, "Fullerite-C28", 2.0, 1),
        (999, 27, "Test Shuttle", 5000.0, 1),
        (1000, 185, "Unpublished Ore", 0.35, 0),
        (1001, 185, "No Volume", None, 1),
    ])
    con.commit()
    con.close()

    catalog = build_volume_catalog(db)
    assert catalog["Plagioclase"] == 0.35
    assert catalog["Plagioclase III-Grade"] == 0.35
    assert catalog["Blue Ice"] == 1000.0
    assert catalog["Fullerite-C28"] == 2.0
    assert "Test Shuttle" not in catalog
    assert "Unpublished Ore" not in catalog
    assert "No Volume" not in catalog

    out = tmp / "sde_volumes.json"
    write_catalog(catalog, out)
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["Plagioclase III-Grade"] == 0.35
    table = OreTable(sde_paths=[out])
    assert table.unit_volume("Plagioclase III-Grade") == 0.35

    print("test_sde: OK")


if __name__ == "__main__":
    main()
