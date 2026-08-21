# AGENTS.md

Ore Hold Watcher — a local Windows tray app that tails EVE Online gamelogs,
estimates each character's ore hold fill, and alerts before it overflows.
Python 3.12 + PySide6. No ESI, no network calls except optional price/update
fetches. GPLv3.

## Commands

```bash
.venv/Scripts/python.exe test_engine.py     # engine, parser, rock countdown
.venv/Scripts/python.exe test_scan.py       # survey-scan paste parser
.venv/Scripts/python.exe test_sde.py        # SDE volume catalog
.venv/Scripts/python.exe -m py_compile app.py engine.py   # syntax check

run.bat        # run the app (creates .venv on first use)
build.bat      # Nuitka standalone build -> dist\OreHoldWatcher\
```

Tests are plain scripts with `assert` and a `main()` — **not** pytest. Each
prints `OK` / `ALL TESTS PASSED` and exits non-zero on failure. Run all three
before claiming done. CI ([.github/workflows/ci.yml](.github/workflows/ci.yml))
runs `compileall` + `test_engine.py` on Linux, so anything it covers must
import without Qt and without Windows.

## Layout

| File | Role |
|---|---|
| [engine.py](engine.py) | Log tailing, parsing, character state, rock countdown. **No Qt.** |
| [scan.py](scan.py) | Survey-scanner paste parser. Pure functions, no state, no I/O. |
| [ores.py](ores.py) | Static m³/unit fallback table. |
| [sde.py](sde.py) | Generates `sde_volumes.json` from CCP's SDE at build time. |
| [app.py](app.py) | All Qt: tray, rows, dialogs, alerts, tick loop. |
| [updater/](updater/) | Rust side-by-side updater (folder swap). |
| [docs/](docs/) | Design docs. Comments cite these as "spec D4", "spec F5". |

**The Qt boundary is the main architectural rule.** Logic goes in `engine.py`
or `scan.py` so it can be tested headlessly; `app.py` renders it. When adding
behaviour, ask whether it can be a pure function or a `CharacterState` method
first — if yes, put it there and test it.

`app.py` is large. Locate work with `grep -n` rather than reading it whole.

## Conventions

- SPDX header + module docstring on every file. Match the existing style.
- ~79 columns. Follow the file you're in.
- **Comments explain *why*, not *what*.** This codebase documents the reasoning
  behind a threshold or an ordering, especially where the obvious approach is
  wrong. Preserve that when editing; a rewritten block that drops the rationale
  is a regression.
- Rationale lives with the code, not just in the commit message.
- Qt styling comes from `DARK_QSS` in [app.py](app.py). Colour is semantic:
  green <75%, amber 75–90%, red >90% = **hold fill**; blue = **rock countdown**,
  never fill. Don't introduce a new colour without a reason.
- User-visible strings say what is happening and what to do. "measuring…",
  "stalled: mining Zeolites" — never a bare dash, which reads as a broken app.

## Domain invariants

These are the things that break silently if you get them wrong.

- **Log timestamps are UTC** (EVE time). Use `now_ts()` / `ts_to_epoch()`, never
  `time.localtime`. They're zero-padded, so string comparison sorts correctly.
- **Replay must be idempotent.** Startup re-reads the lookback window from byte
  0, and `recalculate()` does it mid-session. Any new accumulator needs a
  guard. The pattern: **persist the anchor, never the running total** —
  `anchor_ts`/`anchor_m3` are saved and `est_m3` is rebuilt; `scan_ts` is saved
  and `depleted_units` is rebuilt. Saving a total that the replay also re-adds
  doubles it on every restart.
- **Every mining line names its ore** — including ice. Use it; don't infer.
  The one exception is the residue line, which names nothing and is paired to
  the most recent tick within `RESIDUE_PAIR_S`.
- **Residue never enters the hold** but does deplete the asteroid: it must not
  touch `est_m3`, and must count against a tracked rock.
- **"Critical mining success!" is bonus yield**, logged as its own line beside
  the cycle's normal tick. Additive, never a restatement — count it in the
  hold, against the scanned rock, and in the ledger.
- **A log timestamp is not a unique key.** Resolution is one second, and a
  mining second is rarely one tick: every laser reports separately and a crit
  always shares its second with the tick it bonuses. A line's identity is
  `(filename, line number)` — gamelogs are one append-only file per client
  session, never rotated or truncated, so line N is always line N. The ledger
  resumes from that; `ts <= mark` cost 45% of a real session.
- **The ledger is the only thing that cannot rebuild itself** (it accumulates
  past the lookback window), so it is the only thing that persists a read
  position. Everything else replays and recomputes. Don't add a second one
  without that justification.
- **Log sentences are rarely module-specific.** "deactivates as it finds the
  resource" is said by drones, strip miners and ice harvesters alike. Match the
  sentence, not the module name, or you silently exclude whole ship classes.
- **Ticks arrive in bursts, not one at a time.** Multiple lasers land within
  seconds. Rates are measured per *cycle* via `tick_bursts()`, and the first
  burst contributes no units to the numerator — it was removed before the
  window opened. Counting raw lines overstates the evidence and the rate.
- **Prefer showing nothing to showing a confident wrong number.** Warm-up
  states are reported honestly rather than guessed at.
- Ore names carry grade suffixes (`Plagioclase II-Grade`) and prefixes
  (`Compressed`, `Brimful`). `OreTable` handles them; don't hand-strip.

## Runtime files — do not commit or "fix"

`settings.json`, `state.json`, `ledger.json`, `prices.json`, `debug.log*`,
`sde_volumes.json` are gitignored live user data that exists in the working
tree. Read them to diagnose; never treat them as fixtures or check them in.
`settings.json` contains a real Discord webhook — don't echo it into output.

## Verifying against real data

Real gamelogs are the best test fixture available. The folder is whatever
`log_dir` points at in `settings.json` — EVE's default is
`Documents\EVE\logs\Gamelogs`, which OneDrive often redirects, so read the
setting rather than assuming the path.

Point an `Engine` at that directory with a scratch `state_path` and `poll()`
to check parser or rate changes against genuine ticks. Do this for anything
touching timing or rates — it has caught bugs the unit tests agreed with.

For Qt work, render to a file and look at it rather than guessing:

```python
app = QApplication([]); pm = make_gauge_pixmap(48, 0.62); pm.save("out.png")
```

Delete such artifacts afterwards.
