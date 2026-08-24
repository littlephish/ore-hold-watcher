# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LittlePhish
"""Download and extract the resource-volume subset of the EVE SDE."""
from __future__ import annotations

import gzip
import json
import os
import shutil
import sqlite3
import tempfile
import urllib.request
from pathlib import Path

SDE_URL = "https://www.fuzzwork.co.uk/dump/latest-sqlite.db.gz"
RESOURCE_CATEGORIES = {
    "asteroid",
    "ice",
    "gas cloud",
    "mineral",
    "planetary resources",
}
RESOURCE_GROUP_MARKERS = ("ore", "ice", "gas", "cloud")


def build_volume_catalog(db_path: Path) -> dict[str, float]:
    """Return published resource type names mapped to m3 per unit."""
    query = """
        SELECT t.typeName, t.volume
        FROM invTypes AS t
        JOIN invGroups AS g ON g.groupID = t.groupID
        JOIN invCategories AS c ON c.categoryID = g.categoryID
        WHERE t.published = 1
          AND t.volume IS NOT NULL
          AND t.volume > 0
          AND (
              lower(c.categoryName) IN ({categories})
              OR lower(g.groupName) LIKE '%ore%'
              OR lower(g.groupName) LIKE '%ice%'
              OR lower(g.groupName) LIKE '%gas%'
              OR lower(g.groupName) LIKE '%cloud%'
          )
    """.format(categories=",".join("?" for _ in RESOURCE_CATEGORIES))
    categories = sorted(RESOURCE_CATEGORIES)
    con = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
    try:
        rows = con.execute(query, categories).fetchall()
    finally:
        con.close()
    return {str(name): float(volume) for name, volume in rows if name}


def write_catalog(catalog: dict[str, float], path: Path) -> None:
    """Write a deterministic catalog, replacing the destination atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(dict(sorted(catalog.items(), key=lambda item: item[0].lower())),
                      out, indent=1)
            out.write("\n")
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def update_catalog(path: Path, url: str = SDE_URL) -> int:
    """Download the compressed SDE, extract its resource catalog, and save it."""
    # Windows security/indexing tools can briefly retain the extracted SQLite
    # file after the query closes. Do not turn a successful catalog refresh
    # into an error solely because cleanup races that transient lock.
    with tempfile.TemporaryDirectory(prefix="orewatcher-sde-",
                                      ignore_cleanup_errors=True) as work:
        archive = Path(work) / "sde.db.gz"
        database = Path(work) / "sde.db"
        request = urllib.request.Request(url, headers={"User-Agent": "Ore Hold Watcher"})
        with urllib.request.urlopen(request, timeout=300) as response, archive.open("wb") as out:
            shutil.copyfileobj(response, out)
        with gzip.open(archive, "rb") as source, database.open("wb") as out:
            shutil.copyfileobj(source, out)
        catalog = build_volume_catalog(database)
    if not catalog:
        raise ValueError("SDE contained no resource volumes")
    write_catalog(catalog, Path(path))
    return len(catalog)
