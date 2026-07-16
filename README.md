# Ore Hold Watcher

A fully local, tray-resident EVE Online ore hold tracker — the "whale watcher"
Discord bot, minus Discord. It tails your EVE **gamelogs**, estimates every
character's ore hold fill, shows a small dark fleet-cargo window, and pops a
Windows notification when a hold crosses the alert threshold (default 90%).

No ESI, no API keys, no network access. It only reads the log files EVE
already writes to `Documents\EVE\logs\Gamelogs`.

## Quick start (no packaging)

1. Install Python 3.10+ from python.org (check "Add to PATH").
2. Double-click `run.bat`. First run creates a virtualenv and installs
   PySide6 + winotify, then launches the app straight to the tray + window.

## Build a standalone exe

Double-click `build.bat`. It installs Nuitka into the venv and compiles
`build\OreHoldWatcher.exe` (one file, no console window). First build takes
several minutes.

**Start with Windows:** press `Win+R`, type `shell:startup`, and drop a
shortcut to `OreHoldWatcher.exe` (or `run.bat`) in that folder.

## How it works / using it

- Characters appear automatically as soon as their gamelog shows mining
  activity. Each mining cycle line is converted units → m³ using a built-in
  ore volume table (variants like *Concentrated Veldspar* resolve to their
  base ore; *Compressed* ores use compressed volumes; ice and moon ore and
  gas are included).
- The number is an **estimate accumulated since your last reset** — EVE's
  logs don't say when you unload. When a hauler empties a hold (or you
  compress), right-click the character row → **Reset**, or use **Reset all**
  after an op. You can also right-click → **Set current m³…** to calibrate
  to what the client shows, and **Set capacity…** per character (default
  180,000 m³, changeable in Settings).
- The **Recalculate** button (window and tray) rebuilds every estimate
  purely from the logs: anchors are dropped and the whole lookback window is
  replayed — compressions act as natural zero-points, so this lands on
  reality for a compress-and-move workflow. The tray also keeps a
  "Reset all holds to 0" for after a full unload, and per-character
  right-click → Reset still means "this hold was emptied".
- **Restarts recalculate, never double-count.** Only your reset/calibration
  anchor (timestamp + m³) is saved; on every launch the estimate is rebuilt
  by replaying log events newer than the anchor. Closing and reopening the
  app always lands on the same number.
- **In-ship compression is tracked automatically.** A
  `Successfully compressed X into N Compressed X` log line shrinks the
  estimate by the right amount (compression is 1:1 by units; compressed ore
  is ~1/100 the volume, ice 1/10) and re-arms the alert. Verified against
  real gamelogs. By default the app assumes you drag the compressed ore out
  of the ore hold (fleet hangar etc.) right after compressing, so the full
  raw volume is freed — the game writes no log line for hangar drags, so
  this assumption stands in for it. Untick "Compressed ore is moved out of
  the ore hold" in Settings if you leave compressed stacks in the hold.
  Lines like `<pilot> compressed N X using your compression services` (seen
  by the boosting ship) are deliberately ignored.
- A `(notify) Your ore hold is full` line in the log snaps that character to
  100% and alerts immediately, regardless of the estimate.
- Alerts fire once per crossing and re-arm after a reset or when the fill
  drops 5% below the threshold.
- **Time-to-full estimates:** each actively-mining character's row shows
  "⏳ 2h 14m" based on their mining rate over the last 10 minutes, and the
  status line (plus tray tooltip) shows which hold fills first fleet-wide.
  A pilot with no mining cycle for 5+ minutes shows no ETA.

## Alert methods

Alerts are **fleet digests**: one notification listing every character
(`Name — ~X / Y m³ (Z%)`, fullest first), sent when any character crosses
the threshold, and rate-limited to at most one per "Min. time between
alerts" (default 5 minutes; 0 = every crossing; an alert suppressed by the
limit is sent automatically the moment the window reopens). The **Send test
alert** button sends the real current fleet state through every enabled
method.

## Downtime auto-close (optional, OFF by default)

Tick "Force-close all EVE clients before daily downtime" and set the lead
time (default 5 min). X minutes before the 11:00 UTC cluster shutdown the
app runs `taskkill /F /IM exefile.exe` (process names configurable via
`eve_process_names` in settings.json), then sends an alert saying how many
clients it closed. Fires once per day, only while the app is running.
Force-kill means no graceful logout — anything not yet saved server-side at
that moment is handled by EVE's normal emergency-warp/logoff rules.

Each is an independent checkbox in Settings (saved to
`%APPDATA%\OreHoldWatcher\settings.json`), with a "Send test alert" button:

- **Pop-up** — Windows toast (tray balloon as fallback).
- **Overlay banner** — red always-on-top banner, top-right of the screen,
  click to dismiss.
- **Sound** — the built-in system exclamation ding.
- **Webhook** — HTTP POST. Paste a Discord webhook URL (Server Settings →
  Integrations → Webhooks → New Webhook → Copy URL) and it sends a
  Discord-formatted `@everyone` message, just like the old bot; any other
  URL gets generic JSON: `{"title", "message", "character", "pct",
  "est_m3", "capacity_m3"}`.
- **Phone push (ntfy.sh)** — install the free ntfy app, subscribe to a
  topic name of your choosing (treat it like a password), enter the same
  topic in Settings. No account needed. This is the practical "SMS"
  substitute; true SMS would need a paid gateway like Twilio or your
  carrier's email-to-SMS bridge.
- Closing the window minimizes to the tray. Left-click the tray icon to
  show/hide; right-click for Reset all / Quit. The tray icon is a gauge of
  your fullest character.

## Files it writes

**Beside the exe** (portable) — or beside `app.py` when running from
source. The old `%APPDATA%\OreHoldWatcher\` location is still checked on
every startup: anything found there and missing here is copied over
automatically, and if the exe's folder isn't writable the app keeps using
APPDATA. All of these are in `.gitignore`:

- `settings.json` — log folder, threshold, default capacity, poll interval,
  notification and always-on-top toggles, and `mining_patterns` (custom
  regexes if CCP ever changes the log wording — patterns need named groups
  `qty` and `ore`).
- `state.json` — per-character estimates, so restarts don't lose progress.
- `ores_override.json` — optional; add `{"Some Ore Name": 5.0}` entries
  (m³ per unit) if the app ever reports an unknown ore.

## CI / Releases (GitHub)

Push the folder to a GitHub repo and two workflows take over:

- **CI** (`.github/workflows/ci.yml`) — every push/PR runs the engine tests
  on Linux, then builds the Windows exe with Nuitka and uploads it as a
  build artifact (Actions tab → run → Artifacts).
- **Release** (`.github/workflows/release.yml`) — pushing a version tag
  builds a version-stamped exe and publishes a GitHub Release with
  `OreHoldWatcher.exe` attached and auto-generated notes:

      git tag v1.0.0
      git push origin v1.0.0

Both are fully non-interactive (as is `build.bat` locally); the first CI
build takes a while, later ones reuse the Nuitka compilation cache.

## Debugging

Tick "Debug logging" in Settings to write `debug.log` (beside the exe):
the watched folder, every log file picked up (with its Listener/character),
compressions, unknown-ore warnings, every mining cycle, and — most
importantly — any `(mining)` lines the parser didn't recognize. Open it
from the tray menu or Settings. **When unticked (default) no log file is
written at all.** The window's status line always shows the watched folder
plus live file/line/event counters either way.

The Gamelogs folder is auto-detected for the active user via the Windows
known-folder API (which follows OneDrive-redirected Documents), the
`%OneDrive%` environment variable, then plain `~/Documents` — set it
explicitly in Settings only if yours lives somewhere else entirely.

## Troubleshooting

- **Nothing shows up:** check Settings (⚙) → Gamelogs folder points at your
  actual `Documents\EVE\logs\Gamelogs`, and that you've mined since starting
  the app (it reads logs from the last 24 h). EVE must have
  "Log game messages to a file" enabled (Esc → Chat/logging settings).
- **Numbers drift from reality:** logs can't see jettisons, fleet hangar
  transfers, or compression — use **Set current m³…** to re-sync, and
  **Reset** whenever a hold is emptied.
- **Unknown ore toast:** add that ore to `ores_override.json` and it will be
  counted from then on.
