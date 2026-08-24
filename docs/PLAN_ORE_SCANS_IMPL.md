# Scanned-Rock Countdown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Paste EVE survey-scanner results into Ore Hold Watcher and get a live per-pilot countdown to the locked asteroid running dry.

**Architecture:** A new pure-Python parser (`scan.py`) turns a pasted scan into validated rows. `engine.py` gains a `ResidueEvent` and a `TargetRock` anchored on `CharacterState`, depleting from gamelog ticks. `app.py` gains a paste dialog, pilot inference, and a second line in `CharRow`.

**Tech Stack:** Python 3.10+, PySide6 (Qt), stdlib only. No new dependencies. No network access.

**Spec:** [`docs/PLAN_ORE_SCANS.md`](PLAN_ORE_SCANS.md) — read it before starting. The plan argues from the spec; findings F1–F5 and decisions D1–D6 are referenced by number throughout.

## Global Constraints

- **Python 3.10+**, stdlib only. No new third-party dependencies (spec: Non-goals).
- **No network access.** This feature adds none (spec: Non-goals).
- **GPLv3 headers.** Every new `.py` file starts with `# SPDX-License-Identifier: GPL-3.0-or-later` then `# Copyright (C) 2026 LittlePhish`, matching `engine.py` and `ores.py`. (The MIT `updater/` folder is unrelated — do not copy its header.)
- **Tests are plain scripts, not pytest.** `test_engine.py` defines `main()` with bare `assert`s and a module-level `approx(a, b, tol=0.01)` helper, run as `python test_engine.py`. New tests follow that exact style.
- **Residue must never change `est_m3`** (spec F1). This is the single highest-risk regression in the feature.
- **Hold accounting is unchanged.** Every existing assertion in `test_engine.py` must still pass after every task.
- **Ore names resolve through `OreTable.unit_volume()`** — never hardcode m³ values (spec F4).
- **The 1.25× residue ratio is measured, never hardcoded** (spec F1).

---

### Task 1: Scan paste parser (`scan.py`)

**Files:**
- Create: `scan.py`
- Test: `test_scan.py`

**Interfaces:**
- Consumes: `OreTable` from `engine.py` (has `.unit_volume(name) -> float | None`)
- Produces:
  - `ScanRow` frozen dataclass: `ore: str`, `units: int`, `m3: float`, `isk: float`, `distance_m: float`
  - `parse_scan(text: str, table: OreTable) -> tuple[list[ScanRow], list[str]]` — returns `(rows, warnings)`, never raises

- [ ] **Step 1: Write the failing test**

Create `test_scan.py`:

```python
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

    print("test_scan: OK")


def approx(a, b, tol=0.01):
    assert abs(a - b) < tol, f"{a} != {b}"


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python test_scan.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'scan'`

- [ ] **Step 3: Write minimal implementation**

Create `scan.py`:

```python
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


def _num(raw: str) -> float:
    """'1,244.50' / '1 244' / '19,096' -> float. Returns 0.0 when empty."""
    cleaned = re.sub(r"[^\d.]", "", raw.replace(",", ""))
    try:
        return float(cleaned) if cleaned else 0.0
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python test_scan.py`
Expected: `test_scan: OK`

- [ ] **Step 5: Verify no regression**

Run: `python test_engine.py`
Expected: existing output, no assertion errors.

- [ ] **Step 6: Commit**

```bash
git add scan.py test_scan.py
git commit -m "feat: parse survey scanner paste into validated rows

Rejects rows whose m3 column disagrees with units x unit_volume, so a
mispaste surfaces as a warning instead of a wrong countdown."
```

---

### Task 2: Residue events (`engine.py`)

Spec F1/F2. Residue depletes the asteroid but never enters the hold, and the log line does not name its ore — it must be paired to the preceding tick.

**Files:**
- Modify: `engine.py` — add `RESIDUE_RE` near `COMPRESS_RE` (~line 70), `ResidueEvent` after `CompressionEvent` (~line 176), pairing state in `Engine.__init__`, emit in `_parse_line` **before** the `EXCLUDE_MARKERS` check (~line 697)
- Modify: `test_engine.py`

**Interfaces:**
- Consumes: `LINE_RE`, `TAG_RE`, `parse_qty`, `ts_to_epoch` (all existing in `engine.py`)
- Produces:
  - `ResidueEvent` dataclass: `character: str`, `qty: int`, `ore: str`, `ts: str`
  - `RESIDUE_PAIR_S = 2.0` module constant
  - `Engine._last_tick: dict[str, tuple[str, str]]` mapping character → `(ts, ore)`

- [ ] **Step 1: Write the failing test**

In `test_engine.py`, add this module-level fixture after `LINES_C`:

```python
# Residue: depletes the asteroid but never enters the hold (spec F1).
# The residue line carries no ore name and must pair with the tick above it.
LINES_D = HEADER.format(name="Yuri Urt") + "\n".join([
    "[ 2026.07.15 15:00:00 ] (mining) You mined 13 units of Brimful Coesite",
    "[ 2026.07.15 15:00:00 ] (mining) Additional 13 units depleted from asteroid as residue",
    "[ 2026.07.15 15:01:00 ] (mining) You mined 14 units of Brimful Coesite",
    # >2s after any tick: unpaired, must be discarded
    "[ 2026.07.15 15:05:30 ] (mining) Additional 99 units depleted from asteroid as residue",
]) + "\n"
```

Add to `main()`, after the existing `Diese Nusse` assertions:

```python
    # --- residue (spec F1/F2) ---
    residues = [e for e in events if isinstance(e, ResidueEvent)]
    assert len(residues) == 1, f"expected 1 paired residue, got {len(residues)}"
    assert residues[0].qty == 13
    assert residues[0].ore == "Brimful Coesite", residues[0].ore
    assert residues[0].character == "Yuri Urt"

    # REGRESSION (spec F1): residue must never enter the ore hold.
    yuri = eng.char("Yuri Urt")
    approx(yuri.est_m3, (13 + 14) * 10.0)
```

Add `ResidueEvent` to the `from engine import ...` line at the top of the file.

Register the fixture in `main()` alongside the other log files:

```python
    (tmp / "20260715_150000_93333333.txt").write_bytes(
        b"\xef\xbb\xbf" + LINES_D.encode("utf-8"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python test_engine.py`
Expected: FAIL with `ImportError: cannot import name 'ResidueEvent'`

- [ ] **Step 3: Write minimal implementation**

In `engine.py`, add after `COMPRESS_RE` (~line 74):

```python
# "(mining) Additional 13 units depleted from asteroid as residue"
# Verified in real gamelogs. These units leave the ASTEROID but never enter
# the ore hold, so they must not touch est_m3 - but they do count against a
# scanned rock. The line carries no ore name; it is paired to the character's
# most recent mining tick (see RESIDUE_PAIR_S).
RESIDUE_RE = re.compile(
    rf"Additional\s+(?P<qty>{_NUM})\s+units?\s+depleted\s+from\s+asteroid",
    re.IGNORECASE,
)

# Max gap between a mining tick and the residue line belonging to it. In real
# logs the pair lands within the same timestamp second.
RESIDUE_PAIR_S = 2.0
```

Add after `CompressionEvent` (~line 176):

```python
@dataclass
class ResidueEvent:
    character: str
    qty: int          # units removed from the asteroid, NOT added to the hold
    ore: str          # inferred from the preceding mining tick
    ts: str
```

In `Engine.__init__`, alongside the other per-character dicts:

```python
        # character -> (ts, ore) of the most recent mining tick, used to give
        # the ore-less residue line an ore name
        self._last_tick: dict[str, tuple[str, str]] = {}
```

In `_parse_line`, insert **before** the `if any(k in low for k in EXCLUDE_MARKERS)` line (~697) — placement matters, `EXCLUDE_MARKERS` contains "residue" and would otherwise drop it:

```python
        rm = RESIDUE_RE.search(msg)
        if rm:
            qty = parse_qty(rm.group("qty"))
            last = self._last_tick.get(character)
            if qty <= 0 or not last:
                return None
            last_ts, last_ore = last
            gap = ts_to_epoch(m.group("ts")) - ts_to_epoch(last_ts)
            if not (0 <= gap <= RESIDUE_PAIR_S):
                return None
            return ResidueEvent(character=character, qty=qty, ore=last_ore,
                                ts=m.group("ts"))
```

In `_parse_line`, record every tick — replace the existing `MiningEvent` return (~line 711) with:

```python
            self._last_tick[character] = (m.group("ts"), ore)
            return MiningEvent(character=character, qty=qty, ore=ore,
                               m3=qty * vol, ts=m.group("ts"))
```

In `poll()`, add to the isinstance chain after the `UnknownOreEvent` branch. **`est_m3` is deliberately untouched:**

```python
                elif isinstance(ev, ResidueEvent):
                    self.stats["residue_events"] = (
                        self.stats.get("residue_events", 0) + 1)
                    # NOTE: no est_m3 change - residue never enters the hold.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python test_engine.py`
Expected: all assertions pass, including the `est_m3` regression.

- [ ] **Step 5: Commit**

```bash
git add engine.py test_engine.py
git commit -m "feat: emit ResidueEvent for asteroid residue

Residue depletes the rock but never enters the hold, and the log line has
no ore name, so it pairs with the preceding tick within 2s. Matched before
EXCLUDE_MARKERS, which would otherwise drop it. Covered by a regression
asserting est_m3 is unaffected."
```

---

### Task 3: Target rock and countdown (`engine.py`)

Spec D1/D4 and the warm-up rule.

**Files:**
- Modify: `engine.py` — `TargetRock` after `ResidueEvent`, fields + methods on `CharacterState` (~line 291), `_apply_depletion` on `Engine`, call site in `poll()`
- Modify: `test_engine.py`

**Interfaces:**
- Consumes: `ResidueEvent` (Task 2), `RATE_WINDOW_S`, `RATE_IDLE_S`, `ts_to_epoch`
- Produces:
  - `TargetRock` dataclass: `ore: str`, `scan_units: int`, `scan_ts: str`, `distance_m: float`, `depleted_units: int = 0`
  - `CharacterState.target: TargetRock | None = None`
  - `CharacterState.rock_events: deque` of `(epoch, units)`
  - `CharacterState.rock_remaining() -> int | None`
  - `CharacterState.rock_eta_s(now_epoch=None) -> float | None`
  - `Engine.set_target(character: str, ore: str, units: int, distance_m: float) -> None`
  - `Engine._apply_depletion(ev) -> None`
  - `ROCK_WARMUP_TICKS = 3`, `ROCK_WARMUP_S = 90.0`

- [ ] **Step 1: Write the failing test**

Add to `test_engine.py` `main()`:

```python
    # --- scanned rock countdown (spec D1/D4) ---
    eng.set_target("Yuri Urt", ore="Brimful Coesite", units=1000,
                   distance_m=726.0)
    t = eng.char("Yuri Urt").target
    assert t is not None and t.scan_units == 1000

    # depletion counts mined AND residue (spec F1)
    from engine import MiningEvent as _ME, ResidueEvent as _RE
    eng._apply_depletion(_ME(character="Yuri Urt", qty=100,
                             ore="Brimful Coesite", m3=1000.0,
                             ts="2026.07.15 16:00:00"))
    eng._apply_depletion(_RE(character="Yuri Urt", qty=25,
                             ore="Brimful Coesite",
                             ts="2026.07.15 16:00:00"))
    assert eng.char("Yuri Urt").rock_remaining() == 875, \
        eng.char("Yuri Urt").rock_remaining()

    # a different ore does not deplete this rock
    eng._apply_depletion(_ME(character="Yuri Urt", qty=500, ore="Bitumens",
                             m3=5000.0, ts="2026.07.15 16:00:30"))
    assert eng.char("Yuri Urt").rock_remaining() == 875

    # warm-up: too few ticks to trust a rate (spec: "showing nothing beats
    # showing a confident wrong number")
    assert eng.char("Yuri Urt").rock_eta_s() is None

    # ticks before the scan anchor are ignored
    eng._apply_depletion(_ME(character="Yuri Urt", qty=999,
                             ore="Brimful Coesite", m3=9990.0,
                             ts="2000.01.01 00:00:00"))
    assert eng.char("Yuri Urt").rock_remaining() == 875

    # DroneStopEvent zeroes an observed pop regardless of arithmetic (D4)
    eng.rock_popped("Yuri Urt")
    assert eng.char("Yuri Urt").target is None

    # over-depletion clears rather than going negative
    eng.set_target("Yuri Urt", ore="Coesite", units=10, distance_m=5.0)
    eng._apply_depletion(_ME(character="Yuri Urt", qty=50, ore="Coesite",
                             m3=500.0, ts="2026.07.15 17:00:00"))
    assert eng.char("Yuri Urt").target is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python test_engine.py`
Expected: FAIL with `AttributeError: 'Engine' object has no attribute 'set_target'`

- [ ] **Step 3: Write minimal implementation**

In `engine.py`, add constants next to `RATE_WINDOW_S` (~line 91):

```python
# A rock rate needs a few real cycles before it means anything - the residue
# share varies by crystal and ship, so it is measured, never assumed.
ROCK_WARMUP_TICKS = 3
ROCK_WARMUP_S = 90.0
```

Add after `ResidueEvent`:

```python
@dataclass
class TargetRock:
    """The asteroid a pilot is currently mining, from a survey-scan paste.

    Anchored exactly like CharacterState.anchor_ts/anchor_m3: scan_ts is the
    moment the snapshot was taken, and only ticks newer than it count against
    it, so replaying the logs on restart cannot double-count.
    """
    ore: str
    scan_units: int
    scan_ts: str             # log format "YYYY.MM.DD HH:MM:SS", UTC
    distance_m: float
    depleted_units: int = 0  # mined + residue since scan_ts
```

On `CharacterState`, add fields after `idle_notified`:

```python
    # scanned-rock countdown; not persisted as an object (see save_state)
    target: "TargetRock | None" = None
    # rolling (epoch, units) removed from the rock - mined AND residue
    rock_events: deque = field(default_factory=deque)
```

And methods after `eta_full_s`:

```python
    def rock_remaining(self) -> int | None:
        """Units left in the scanned rock, or None when no rock is targeted."""
        if not self.target:
            return None
        return max(0, self.target.scan_units - self.target.depleted_units)

    def rock_depletion_rate(self, now_epoch: float | None = None) -> float:
        """Units per minute coming off the rock (mined + residue), 0 when idle
        or still warming up."""
        if not self.rock_events:
            return 0.0
        now_epoch = now_epoch if now_epoch is not None else time.time()
        newest = self.rock_events[-1][0]
        if now_epoch - newest > RATE_IDLE_S:
            return 0.0
        oldest = self.rock_events[0][0]
        span = newest - oldest
        # Spec: below the warm-up threshold the rate is noise, not a number.
        if len(self.rock_events) < ROCK_WARMUP_TICKS or span < ROCK_WARMUP_S:
            return 0.0
        total = sum(u for _, u in self.rock_events)
        return total / (span / 60.0)

    def rock_eta_s(self, now_epoch: float | None = None) -> float | None:
        """Seconds until the scanned rock is dry; None when unknown."""
        remaining = self.rock_remaining()
        if not remaining:
            return None
        rate = self.rock_depletion_rate(now_epoch)
        if rate <= 0:
            return None
        return remaining / rate * 60.0
```

On `Engine`, add:

```python
    def set_target(self, character: str, ore: str, units: int,
                   distance_m: float):
        """Anchor a scanned rock to a character. Re-pasting re-anchors."""
        c = self.char(character)
        c.target = TargetRock(ore=ore, scan_units=int(units), scan_ts=now_ts(),
                              distance_m=float(distance_m))
        c.rock_events.clear()
        log.info("target: %s -> %s %d units @ %.0f m",
                 character, ore, units, distance_m)
        self.save_state()

    def clear_target(self, character: str):
        c = self.char(character)
        c.target = None
        c.rock_events.clear()
        self.save_state()

    def rock_popped(self, character: str):
        """An observed pop (drone stop) outranks arithmetic - spec D4."""
        c = self.chars.get(character)
        if c and c.target:
            log.info("target: %s rock popped (observed)", character)
            self.clear_target(character)

    def _apply_depletion(self, ev):
        """Count a mining or residue event against the character's rock.

        Gated on the rock's own scan_ts, independent of the hold anchor, so a
        calibration newer than the scan cannot silently stop depletion.
        """
        c = self.chars.get(ev.character)
        if not c or not c.target or ev.ore != c.target.ore:
            return
        if ev.ts <= c.target.scan_ts:   # log timestamps sort lexicographically
            return
        c.target.depleted_units += ev.qty
        ep = ts_to_epoch(ev.ts)
        if ep:
            c.rock_events.append((ep, ev.qty))
            while (c.rock_events and
                   ep - c.rock_events[0][0] > RATE_WINDOW_S):
                c.rock_events.popleft()
        if c.rock_remaining() <= 0:
            log.info("target: %s rock exhausted by count", ev.character)
            self.clear_target(ev.character)
```

In `poll()`, call it **before** the hold anchor filter (right after `if ev is None: continue`), because the hold anchor is about cargo and must not gate rock depletion:

```python
                if isinstance(ev, (MiningEvent, ResidueEvent)):
                    self._apply_depletion(ev)
                elif isinstance(ev, DroneStopEvent):
                    self.rock_popped(ev.character)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python test_engine.py`
Expected: all assertions pass.

- [ ] **Step 5: Commit**

```bash
git add engine.py test_engine.py
git commit -m "feat: scanned-rock depletion tracking and countdown

Rock depletes by mined + residue units, anchored on scan time so log replay
cannot double-count. Rate is measured from real ticks with a warm-up floor:
below 3 ticks or 90s the ETA is None rather than a guess."
```

---

### Task 4: Persist the target (`engine.py`)

**Files:**
- Modify: `engine.py` — `load_state` (~line 399), `save_state` (~line 418)
- Modify: `test_engine.py`

**Interfaces:**
- Consumes: `TargetRock` (Task 3)
- Produces: `state.json` characters gain an optional `"target"` object with keys `ore`, `scan_units`, `scan_ts`, `distance_m`, `depleted_units`

- [ ] **Step 1: Write the failing test**

Add to `test_engine.py` `main()`:

```python
    # --- target persistence ---
    sp = tmp / "state_target.json"
    e1 = Engine(log_dir=tmp, state_path=sp, default_capacity=180000.0)
    e1.poll()
    e1.set_target("Yuri Urt", ore="Coesite", units=5000, distance_m=726.0)
    e1.char("Yuri Urt").target.depleted_units = 1200
    e1.save_state()

    e2 = Engine(log_dir=tmp, state_path=sp, default_capacity=180000.0)
    t2 = e2.char("Yuri Urt").target
    assert t2 is not None, "target did not survive reload"
    assert t2.ore == "Coesite" and t2.scan_units == 5000
    assert t2.depleted_units == 1200
    approx(t2.distance_m, 726.0)

    # a state file with no target key loads cleanly (backward compatible)
    legacy = tmp / "state_legacy.json"
    legacy.write_text('{"characters": {"Solo Pilot": {"capacity": 180000.0}}}',
                      encoding="utf-8")
    e3 = Engine(log_dir=tmp, state_path=legacy, default_capacity=180000.0)
    assert e3.char("Solo Pilot").target is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python test_engine.py`
Expected: FAIL on `assert t2 is not None, "target did not survive reload"`

- [ ] **Step 3: Write minimal implementation**

In `load_state`, after constructing each `CharacterState`:

```python
            td = d.get("target")
            if isinstance(td, dict) and td.get("ore"):
                self.chars[name].target = TargetRock(
                    ore=str(td["ore"]),
                    scan_units=int(td.get("scan_units", 0)),
                    scan_ts=str(td.get("scan_ts", "")),
                    distance_m=float(td.get("distance_m", 0.0)),
                    depleted_units=int(td.get("depleted_units", 0)),
                )
```

In `save_state`, replace the dict comprehension body so each character emits its target when present:

```python
    def save_state(self):
        chars = {}
        for c in self.chars.values():
            d = {"capacity": c.capacity, "last_event": c.last_event,
                 "notified": c.notified, "anchor_ts": c.anchor_ts,
                 "anchor_m3": c.anchor_m3}
            if c.target:
                d["target"] = {"ore": c.target.ore,
                               "scan_units": c.target.scan_units,
                               "scan_ts": c.target.scan_ts,
                               "distance_m": c.target.distance_m,
                               "depleted_units": c.target.depleted_units}
            chars[c.name] = d
        data = {"characters": chars}
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(data, indent=2),
                                       encoding="utf-8")
        except OSError:
            pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python test_engine.py`
Expected: all assertions pass.

- [ ] **Step 5: Commit**

```bash
git add engine.py test_engine.py
git commit -m "feat: persist scanned rock target in state.json

Optional key; state files without it load unchanged."
```

---

### Task 5: Pilot inference (`app.py`)

Spec D3. Last-focused EVE client, falling back to the ore-tick tiebreak.

**Files:**
- Modify: `app.py` — `ClientWatcher` (~line 393)

**Interfaces:**
- Consumes: existing `ClientWatcher.refresh()` Win32 enumeration
- Produces:
  - `ClientWatcher.last_focused: str | None` — character name of the most recent EVE window seen in the foreground
  - `MainWindow.guess_scan_pilot() -> str | None`

- [ ] **Step 1: Write the implementation**

In `ClientWatcher.__init__`, add:

```python
        self.last_focused: str | None = None   # most recent foreground pilot
```

In `refresh()`, before `user32.EnumWindows(enum_cb, 0)`:

```python
            fg = user32.GetForegroundWindow()
```

Inside `enum_cb`, in the branch where a character name is extracted, replace:

```python
                            if " - " in title:
                                found.add(title.split(" - ", 1)[1].strip())
```

with:

```python
                            if " - " in title:
                                who = title.split(" - ", 1)[1].strip()
                                found.add(who)
                                # remember which client you were last looking
                                # at, so a scan pasted after alt-tabbing still
                                # attributes to the right pilot (spec D3)
                                if hwnd == fg:
                                    self.last_focused = who
```

Add to `MainWindow`:

```python
    def guess_scan_pilot(self) -> str | None:
        """Best guess at which pilot a pasted scan belongs to (spec D3).

        Last-focused EVE client first; if that is unknown or stale, fall back
        to the only pilot currently mining. Ambiguity returns None and the
        dialog leaves its dropdown unselected rather than guessing wrong -
        misattribution corrupts two pilots' countdowns at once.
        """
        who = getattr(self.clients, "last_focused", None)
        if who and who in self.engine.chars:
            return who
        active = [c.name for c in self.engine.chars.values()
                  if c.mining_rate_m3_min() > 0]
        return active[0] if len(active) == 1 else None
```

- [ ] **Step 2: Verify manually**

Run: `python app.py`
Focus an EVE client, then the app. Confirm no exception in `debug.log` and that `ClientWatcher.last_focused` populates (add a temporary `log.info`, then remove it).

On a machine with no EVE running, confirm `guess_scan_pilot()` returns `None` without raising.

- [ ] **Step 3: Verify no regression**

Run: `python test_engine.py && python test_scan.py`
Expected: both pass.

- [ ] **Step 4: Commit**

```bash
git add app.py
git commit -m "feat: track last-focused EVE client for scan attribution

Foreground pilot is remembered during the existing poll, so a scan pasted
after alt-tabbing still lands on the right character."
```

---

### Task 6: Scan paste dialog (`app.py`)

Spec D2/D3.

**Files:**
- Modify: `app.py` — add `ScanPasteDialog` after `OreHoldInfoDialog` (~line 1927), menu entries in `CharRow.menu` and the tray menu

**Interfaces:**
- Consumes: `parse_scan`, `ScanRow` (Task 1); `Engine.set_target` (Task 3); `MainWindow.guess_scan_pilot` (Task 5); existing `DarkDialog`
- Produces: `ScanPasteDialog(main, preselect_pilot: str | None = None)` with `.exec()`

- [ ] **Step 1: Write the implementation**

Add the import at the top of `app.py`, below the existing `from engine import ...` block (line 46-47):

```python
from scan import parse_scan
```

`QPlainTextEdit` and `QComboBox` are **not** currently imported — `QComboBox`
appears only inside stylesheet strings. Add both to the `PySide6.QtWidgets`
import block (lines 38-44):

```python
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog,
                               QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QMainWindow, QMenu, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton,
                               QScrollArea, QSpinBox, QSystemTrayIcon,
                               QVBoxLayout, QWidget, QCheckBox)
```

Add the dialog:

```python
class ScanPasteDialog(DarkDialog):
    """Paste survey-scanner output, pick the rock being mined, arm a countdown.

    The rock is auto-proposed as the nearest one whose ore matches what the
    pilot is mining (spec D2) and the pilot from the last-focused client
    (spec D3). Both are dropdowns: the guess is visible, so being wrong is
    cheap.
    """

    def __init__(self, main: "MainWindow", preselect_pilot: str | None = None):
        super().__init__(main)
        self.main = main
        self.rows: list = []
        self.setWindowTitle("Paste survey scan")
        self.resize(560, 520)

        self.paste = QPlainTextEdit()
        self.paste.setPlaceholderText(
            "Select all rows in the survey scanner window, copy, paste here.")
        self.paste.textChanged.connect(self.reparse)

        self.pilot = QComboBox()
        self.pilot.addItem("- select pilot -", None)
        for name in sorted(main.engine.chars):
            self.pilot.addItem(name, name)
        guess = preselect_pilot or main.guess_scan_pilot()
        if guess:
            i = self.pilot.findData(guess)
            if i >= 0:
                self.pilot.setCurrentIndex(i)

        self.rock = QComboBox()
        self.warn = QLabel("")
        self.warn.setWordWrap(True)
        self.warn.setStyleSheet("color: #f0b232;")

        self.ok = QPushButton("Track this rock")
        self.ok.clicked.connect(self.accept_target)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(self.ok)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Survey scanner results:"))
        lay.addWidget(self.paste, 1)
        lay.addWidget(self.warn)
        lay.addWidget(QLabel("Pilot:"))
        lay.addWidget(self.pilot)
        lay.addWidget(QLabel("Rock being mined:"))
        lay.addWidget(self.rock)
        lay.addLayout(buttons)
        self.reparse()

    def reparse(self):
        text = self.paste.toPlainText()
        self.rows, warnings = parse_scan(text, self.main.engine.table)
        self.rock.clear()
        # nearest first: the locked rock is the close one (spec F5)
        for r in sorted(self.rows, key=lambda r: r.distance_m):
            dist = (f"{r.distance_m:,.0f} m" if r.distance_m < 1000
                    else f"{r.distance_m / 1000:,.0f} km")
            self.rock.addItem(f"{r.ore} · {r.units:,} units · {dist}", r)
        self.preselect_rock()
        if warnings:
            shown = warnings[:3]
            more = (f" (+{len(warnings) - 3} more)" if len(warnings) > 3 else "")
            self.warn.setText(" ".join(shown) + more)
        else:
            self.warn.setText("")
        self.ok.setEnabled(bool(self.rows))

    def preselect_rock(self):
        """Nearest rock whose ore matches what this pilot is mining."""
        who = self.pilot.currentData()
        ore = None
        if who:
            c = self.main.engine.chars.get(who)
            tick = self.main.engine._last_tick.get(who) if c else None
            ore = tick[1] if tick else None
        if not ore or not self.rows:
            return
        for i in range(self.rock.count()):
            row = self.rock.itemData(i)
            if row is not None and row.ore == ore:
                self.rock.setCurrentIndex(i)   # list is nearest-first
                return

    def accept_target(self):
        who = self.pilot.currentData()
        row = self.rock.currentData()
        if not who:
            self.warn.setText("Pick which pilot this scan came from.")
            return
        if row is None:
            self.warn.setText("Pick the rock being mined.")
            return
        self.main.engine.set_target(who, ore=row.ore, units=row.units,
                                    distance_m=row.distance_m)
        self.main.refresh()
        self.accept()
```

(Both `QPlainTextEdit` and `QComboBox` were added to the import block in Step 1.)

In `CharRow.menu` (line 1213), add an action after the "Set capacity…" line:

```python
        m.addAction("Paste survey scan…",
                    lambda: ScanPasteDialog(self.main, self.name).exec())
```

And in the tray menu (line ~2327), after "Recalculate from logs":

```python
        menu.addAction("Paste survey scan…",
                       lambda: ScanPasteDialog(self).exec())
```

- [ ] **Step 2: Verify manually**

Run: `python app.py`, open the dialog, paste the sample from the spec's F5 section.
Expected: 66 rows, rock dropdown sorted nearest-first with `Coesite · 18,611 units · 726 m` at the top and preselected when that pilot is mining Coesite.

Paste deliberate junk. Expected: amber warning text, "Track this rock" disabled, no traceback.

- [ ] **Step 3: Verify no regression**

Run: `python test_engine.py && python test_scan.py`

- [ ] **Step 4: Commit**

```bash
git add app.py
git commit -m "feat: survey scan paste dialog

Rows sorted nearest-first, rock and pilot auto-proposed with dropdown
override, parser warnings surfaced inline."
```

---

### Task 7: Countdown display and soft alert (`app.py`)

Spec D5/D6.

**Files:**
- Modify: `app.py` — `CharRow` (~line 1131), `MainWindow.refresh` (~line 2996), `README.md`

**Interfaces:**
- Consumes: `CharacterState.target`, `.rock_remaining()`, `.rock_eta_s()` (Task 3); existing `fmt_eta`, `fmt_dur`, `OverlayBanner`
- Produces: `CharRow.update_rock(target, remaining, eta_s, pilot_name)`; `MainWindow._rock_warned: dict[str, str]`

- [ ] **Step 1: Write the implementation**

In `CharRow.__init__`, after `lay.addWidget(self.bar)`:

```python
        # Second line, only present when a scanned rock is being tracked
        # (spec D5) - the window grows only when the feature is in use.
        self.rock = QLabel("")
        self.rock.setObjectName("rockLine")
        self.rock.setVisible(False)
        lay.addWidget(self.rock)
```

Add the method:

```python
    ROCK_WARN_S = 60.0    # soft alert threshold (spec D6)
    ROCK_CRIT_S = 20.0

    def update_rock(self, target, remaining, eta_s, pilot_name):
        """Second line: what rock, how much left, how long, how stale."""
        if not target:
            self.rock.setVisible(False)
            return
        age = time.time() - ts_to_epoch(target.scan_ts)
        eta_txt = fmt_eta(eta_s) if eta_s else "—"
        # Anchor age and pilot are always shown: a stale number must look
        # stale (spec D4), and misattribution must be visible (spec D3).
        self.rock.setText(
            f"⛏ {target.ore} · {remaining:,} left · dry in {eta_txt}"
            f" · as of {fmt_dur(age)} · {pilot_name}")
        colour = "#949ba4"
        if eta_s is not None:
            if eta_s <= self.ROCK_CRIT_S:
                colour = "#f23f43"
            elif eta_s <= self.ROCK_WARN_S:
                colour = "#f0b232"
        self.rock.setStyleSheet(f"color: {colour}; font-size: 11px;")
        self.rock.setVisible(True)
```

`ts_to_epoch` is already imported in `app.py` (line 47) — no import change needed.

In `MainWindow.refresh`, next to the existing `row.update_state(...)` call:

```python
            row.update_rock(c.target, c.rock_remaining(), c.rock_eta_s(),
                            c.name)
            self._rock_alert(c)
```

Add to `MainWindow.__init__`:

```python
        # character -> scan_ts already warned about; re-arms on re-anchor
        self._rock_warned: dict[str, str] = {}
```

Add the soft alert. **It must never reach `Notifier`'s webhook, ntfy, sound, or digest paths** — per spec D6 this fires once per rock, potentially every few minutes, and is the reason it stays on this machine:

```python
    def _rock_alert(self, c):
        """Soft, local-only warning that a rock is about to run dry (D6).

        Strip miners get no popped-rock line in the log at all (spec F3), so
        without this a minimised window means no warning. Deliberately not
        routed through Notifier: no sound, no webhook, no ntfy, no digest.
        """
        if not c.target:
            self._rock_warned.pop(c.name, None)
            return
        eta = c.rock_eta_s()
        if eta is None or eta > CharRow.ROCK_WARN_S:
            return
        if self._rock_warned.get(c.name) == c.target.scan_ts:
            return
        self._rock_warned[c.name] = c.target.scan_ts
        # Straight to the overlay widget, NOT through Notifier.alert(), which
        # fans out to popup/sound/webhook/ntfy. Still respects the user's
        # overlay preference.
        if self.settings["notify_overlay"]:
            self.notifier.overlay.show_alert(
                f"{c.name}: {c.target.ore} rock nearly dry")
```

**Do not call `self.notifier.alert(...)` here.** `Notifier.alert()` (app.py:1003)
fans one call out to popup, overlay, sound, webhook, and ntfy according to
settings — exactly the hard-alert behaviour D6 excludes. The overlay widget
lives at `self.notifier.overlay` and its method is `show_alert(text)`.

- [ ] **Step 2: Verify manually**

Run: `python app.py`. With a target set, confirm the second line appears only for that pilot, shows anchor age counting up, and that other rows are unchanged in height.

Force the alert by setting a target with a small unit count while mining. Confirm the banner appears once, the line goes amber then red, and **no** Discord/ntfy message is sent (check `debug.log` for webhook activity).

Clear the target; confirm the line disappears and the row returns to its original height.

- [ ] **Step 3: Update the README**

In the feature list near the top, after the compression/ETA bullet:

```markdown
- Survey-scan rock countdown: paste the scanner results and each pilot gets a
  live "dry in 4m12s" for the rock they're on, counting mined *and* residue
  units (a rock drains ~25% faster than your hold fills). Soft on-screen
  warning only - never sent to Discord or your phone.
```

- [ ] **Step 4: Verify no regression**

Run: `python test_engine.py && python test_scan.py`
Expected: both pass.

- [ ] **Step 5: Commit**

```bash
git add app.py README.md
git commit -m "feat: rock countdown line and soft local alert

Second CharRow line appears only when a rock is tracked, showing anchor age
and attributed pilot so stale or misattributed numbers look wrong. Alert is
overlay-only and never reaches webhook, ntfy, or the digest."
```

---

## Verification

After Task 7, the whole feature should satisfy:

```bash
python test_engine.py    # hold accounting unchanged, residue + rock covered
python test_scan.py      # parser covered
python app.py            # manual: paste, track, watch it count down
```

Manual end-to-end: mine a rock, paste a scan, confirm the countdown falls faster than the hold ETA rises (spec F1 predicts ~1.25×), and that it reaches zero at roughly the moment the rock pops.
