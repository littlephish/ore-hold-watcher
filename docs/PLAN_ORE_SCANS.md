# Scanned-rock countdown — design

**Date:** 2026-08-17
**Status:** approved for planning

## Problem

Ore Hold Watcher answers *when will my hold be full* ([`CharacterState.eta_full_s`](../engine.py)).
It cannot answer the question a miner asks more often: **when does the rock I am
on right now run dry**, so I can pre-lock the next one.

The survey scanner window already knows. It just lives in the game client, as a
static snapshot, with no countdown.

## Goal

Paste the survey scanner results into Ore Hold Watcher. It identifies the rock
being mined, subtracts depletion from the gamelog as it happens, and shows a
live countdown per pilot:

```
⛏ Coesite · 18,611 left · dry in 4m12s
```

## Non-goals

- **Whole-field or session-planning views.** Considered and dropped; the single
  locked rock is the number worth glancing at.
- **ESI / network lookups.** The app is deliberately offline. This feature adds
  no network access.
- **ISK column.** Parsed for validation, never displayed. Pricing already exists.
- **Automatic rock identification without a paste.** The gamelog does not name
  asteroids. A scan paste is required.

---

## Findings that shape the design

These come from the author's real gamelogs (296,311 mining ticks) and from the
sample survey-scanner paste. They are the reason the design looks as it does.

### F1 — Residue depletes the rock but never enters the hold

Two mining line shapes exist:

```
(mining) You mined 13 units of Brimful Coesite
(mining) Additional 13 units depleted from asteroid as residue
```

Residue is correctly excluded from *hold* accounting today, via
`EXCLUDE_MARKERS` in `engine.py`. Those units never reach the ore hold. But they
**do** come off the asteroid.

Measured across the logs:

| | units | ticks |
|---|---|---|
| Mined into hold | 4,113,821 | 296,311 |
| Residue (rock only) | 1,036,745 | 67,663 |

**The rock depletes at 1.25× the hold fill rate.** A countdown built on
`You mined` alone reads 8m00s where the truth is 6m24s — always optimistic, by
~25%, permanently. Counting residue is not a refinement; without it the feature
is wrong in the direction that makes you miss the pop.

The ratio is **not hardcoded**. It varies with crystal type, ship, and skills, so
it is computed live from the pilot's own recent ticks.

### F2 — The residue line does not name its ore

`Additional 13 units depleted from asteroid as residue` has no ore name. It must
be paired with the tick it belongs to. In the logs the pair lands within the same
timestamp second.

### F3 — Strip miners emit no depletion signal

Searching every gamelog for mining-module deactivations returns nothing — only
missile and turret combat lines. `DRONE_STOP_RE` ("pale shadow of its former
glory") is the **only** rock-popped signal in the log, and it is drone-only.

Consequences: the countdown is the sole warning for strip mining, which is why it
gets a soft alert; and the self-correction in D4 below is drone-only.

### F4 — Ore variants already resolve

`Brimful` / `Glistening` prefixes resolve through the existing suffix rule in
`ores.py` (`Brimful Coesite` → `Coesite` → 10 m³). Verified against every row of
the sample paste. **No ore-table changes.**

### F5 — Distance identifies the locked rock

In the sample scan, one rock sits at `726 m` and every other rock is ≥16 km. The
locked rock is the near one. Additionally, untouched rocks in that belt cap at
19,096 units; the target reads 18,611, i.e. already being mined — corroborating
evidence the proximity guess is right.

---

## Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | Countdown targets a **single locked rock** per pilot | The number worth glancing at |
| D2 | **Auto-propose** nearest rock matching the mined ore; dropdown override | Zero clicks when right, visibly wrong when wrong |
| D3 | **Auto-attribute** pilot: last-focused EVE client, tiebreak on ore ticks; dropdown override | Two heuristics fail in different cases |
| D4 | **Anchor at paste time**; self-correct on `DroneStopEvent` and zero-crossing | Error bounded by alt-tab latency, errs optimistic |
| D5 | Display as a **second line** under the bar, only when a target exists | Window grows only when the feature is in use |
| D6 | **Soft alert** — local and visual; never broadcast | Warns without joining the hard-alert channels |

### On D3: misattribution is the worst failure

A wrong *rock* gives one wrong countdown, and you find out when it pops early. A
wrong *pilot* corrupts a second pilot's countdown too — one paste, two bad
numbers, no on-screen explanation. Therefore the attributed pilot is shown on the
target line itself, not just in the paste dialog.

### On D4: what cannot be fixed

If a fleetmate mines your rock, their units never appear in your log. The
countdown reads high and **no local signal corrects it**. This is a property of
an offline design, not a bug to engineer away.

Mitigation is honesty: the target line shows anchor age ("as of 4m"), so a stale
number looks stale, and re-pasting a fresh scan re-anchors to truth in one
gesture.

| Failure | Fix |
|---|---|
| Wrong rock guessed | dropdown → pick the row |
| Wrong pilot guessed | dropdown → pick the pilot |
| Anchor drift / fleetmate ate the rock | re-paste a fresh scan |
| Rock popped (drones) | `DroneStopEvent` zeroes it automatically |

---

## Architecture

Three units with one job each.

### `scan.py` (new) — paste parsing

Pure functions. No Qt, no engine imports, no I/O. Fully testable in isolation.

```python
@dataclass(frozen=True)
class ScanRow:
    ore: str
    units: int
    m3: float
    isk: float
    distance_m: float   # normalised from "726 m" / "92 km"

def parse_scan(text: str, table: OreTable) -> tuple[list[ScanRow], list[str]]
```

Returns rows plus human-readable warnings. Behaviour:

- Tab-separated, five columns. Tolerates thousands separators, the `m3` and
  `ISK` suffixes, and `m` vs `km` distances.
- **Validates `units × unit_volume(ore) ≈ m3`** (0.5% tolerance). A row that
  fails is dropped with a warning rather than silently trusted — a mispaste
  should not produce a confident wrong countdown.
- Unknown ore → row dropped, warning names the ore, consistent with the existing
  `UnknownOreEvent` path.
- Empty or wholly unparseable input → `([], [warning])`, never an exception.

### `engine.py` — depletion tracking

**New event.** `ResidueEvent(character, qty, ts)`. Emitted by matching the
residue line *before* `EXCLUDE_MARKERS` drops it. Per F2 the line carries no ore,
so the engine attributes it to the ore of the most recent `MiningEvent` for that
character within a 2-second window; unpaired residue is discarded.

> This is the one change to existing parse behaviour. Hold accounting **must**
> stay unchanged — residue still never adds m³ to a hold. Regression coverage is
> required.

**New state.** `TargetRock` on `CharacterState`:

```python
@dataclass
class TargetRock:
    ore: str
    scan_units: int          # units at scan time
    scan_ts: str             # log-format UTC, the anchor
    distance_m: float
    depleted_units: int = 0  # mined + residue since scan_ts
```

This deliberately mirrors the existing `anchor_ts` / `anchor_m3` pattern, so
startup log replay recomputes it instead of double-counting.

**Derived values.**

```
remaining      = max(0, scan_units - depleted_units)
depletion_rate = (mined + residue units of that ore) / minute, rolling window
eta_dry_s      = remaining / depletion_rate * 60
```

Reuses the existing `RATE_WINDOW_S` (600s) and `RATE_IDLE_S` (300s) so the
countdown goes quiet when the hold ETA does.

**Warm-up.** Below a minimum sample (3 ticks *or* 90s of data), `eta_dry_s`
returns `None` and the UI shows `—`. Per F1 the residue ratio is measured, not
assumed, and a rate derived from one tick is noise. **Showing nothing beats
showing a confident wrong number.**

**Self-correction (D4).** `DroneStopEvent` for a pilot with a target zeroes
`remaining` outright — an observed pop outranks arithmetic. Reaching zero by
count clears the target and stops the countdown rather than displaying negatives.

**Persistence.** `TargetRock` serialises into `state.json` under each character
alongside `anchor_ts` / `anchor_m3`. Absent key → no target; old state files load
unchanged.

### `app.py` — UI

**Paste dialog** (`ScanPasteDialog`, a `DarkDialog`): textarea → parsed table,
pilot dropdown (auto-proposed per D3), rock dropdown (auto-proposed per D2, rows
sorted nearest-first and labelled `Coesite · 18,611 · 726 m`). Warnings from
`parse_scan` shown inline. Opened from the `CharRow` context menu and the tray
menu.

**Foreground tracking** for D3: `ClientWatcher` gains a `last_focused` field,
recording the most recent EVE window seen in the foreground during its existing
poll. No new dependency — same read-only Win32 calls it already makes. When
stale or absent, fall back to the ore-tick tiebreak, then to the dropdown.

**Target line** (D5): a second row inside `CharRow`, visible only when a target
exists.

```
⛏ Coesite · 18,611 · dry in 4m12s · as of 4m · Diese Nusse
```

Anchor age and pilot are always present, per D3 and D4.

**Soft alert** (D6). Two tiers, both local:

- **Passive** — the target line turns amber under 60s and red under 20s, reusing
  `fill_color`'s palette.
- **Soft notice** — at the 60s threshold, one `OverlayBanner` per rock, silent,
  auto-dismissing. This is the tier that earns its place: per F3, strip miners
  get **no** popped-rock signal from the log, so without it a minimised window
  means no warning at all.

**Explicitly excluded:** sound, Discord/webhook, ntfy phone push, and the fleet
digest. Per F3 this fires once per rock — potentially every few minutes in a moon
belt — which is precisely the cadence that would train you to ignore the
hold-full alert if it were allowed to reach your phone. The rule: this alert
never leaves the machine.

Rate-limited to one notice per pilot per rock; re-anchoring via a fresh paste
re-arms it.

---

## Data flow

```
survey scan paste
      │
      ▼
 parse_scan()  ──► rows + warnings
      │
      ▼
 ScanPasteDialog ── pilot (D3) ──┐
      │            rock  (D2)    │
      ▼                          ▼
            CharacterState.target = TargetRock(scan_ts = now)
                       │
   gamelog ────────────┤
     "You mined N"  ───┤──► MiningEvent  ─┐
     "Additional N" ───┤──► ResidueEvent ─┤► depleted_units += N   (ore matches)
     "pale shadow"  ───┤──► DroneStopEvent ──► remaining = 0
                       │
                       ▼
              remaining / depletion_rate ──► CharRow target line
```

## Error handling

| Case | Behaviour |
|---|---|
| Malformed paste | Rows dropped with named warnings; valid rows still usable |
| Unknown ore in paste | Row dropped, ore named, matches existing unknown-ore path |
| `units × volume ≠ m3` | Row dropped — a mispaste must not become a countdown |
| No pilot inferable | Dropdown, no preselection; paste cannot be committed without one |
| Mining an ore the target isn't | Ticks ignored for depletion; target unchanged |
| Rate below warm-up | `—`, never a guess |
| Remaining hits 0 | Target cleared, countdown removed |
| Old `state.json` | Missing target key loads as no target |

## Testing

`test_engine.py` runs as plain `python test_engine.py` with bare assertions. New
cases follow that style.

**`scan.py`** — the full 66-row sample paste as a fixture; verifies row count,
`m` vs `km` normalisation (the 726 m row), thousands separators, `Brimful` /
`Glistening` suffix resolution (F4), volume-mismatch rejection, unknown-ore
warning, and empty input.

**Residue pairing (F1/F2)** — residue attributed to the preceding tick's ore;
residue beyond the 2s window discarded; **regression: residue never changes
`est_m3`**.

**Depletion math** — `scan_units − (mined + residue)`; rate includes residue;
`DroneStopEvent` zeroes remaining; zero-crossing clears the target; warm-up
returns `None`.

**Persistence** — target survives save/load; log replay after restart does not
double-count; pre-existing `state.json` loads unchanged.

**Attribution (D3)** — ore-tick tiebreak picks the right pilot; ambiguity leaves
the dropdown unselected.

**Soft alert (D6)** — fires once per pilot per rock at the 60s threshold;
re-arms on re-anchor; **regression: never reaches `Notifier`'s webhook, ntfy, or
digest paths.**

## Files touched

| File | Change |
|---|---|
| `scan.py` | new — paste parser |
| `engine.py` | `ResidueEvent`, residue pairing, `TargetRock`, depletion + rate, persistence |
| `app.py` | `ScanPasteDialog`, `ClientWatcher.last_focused`, `CharRow` target line, soft alert |
| `test_engine.py` | residue, depletion, persistence, attribution cases |
| `test_scan.py` | new — parser cases |
| `README.md` | feature documentation |

## Open questions

None. Alerting was resolved as soft/visual-only (D6); the residue ratio is
measured rather than configured; field and session views are out of scope.
