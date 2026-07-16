# Ore Hold Watcher

A tray-resident ore hold tracker for EVE Online. It tails your gamelogs,
estimates each character's ore hold fill, shows a small dark fleet window,
and alerts you when a hold crosses your threshold (90% by default). It runs
entirely on your machine: no Discord bot, no ESI, no API keys, no network
access beyond the alert methods you turn on. The only input is the log
files EVE already writes to `Documents\EVE\logs\Gamelogs`.

## Quick start (no packaging)

1. Install Python 3.10+ from python.org and check "Add to PATH".
2. Double-click `run.bat`. The first run creates a virtualenv, installs
   PySide6 and winotify, then launches straight to the tray and window.

## Build a standalone exe

Double-click `build.bat`. It installs Nuitka into the venv and compiles
`build\OreHoldWatcher.exe` as a single file with no console window. The
first build takes several minutes and needs no key presses.

To start it with Windows: press `Win+R`, type `shell:startup`, and drop a
shortcut to the exe (or `run.bat`) in that folder.

## How it works

Characters appear automatically once their gamelog shows mining activity.
Each mining cycle line is converted from units to m³ using a built-in ore
volume table. Variants like Concentrated Veldspar resolve to their base
ore, Compressed ores use compressed volumes, and ice, moon ore, and gas are
included.

The number shown is an estimate accumulated since your last reset, because
EVE's logs never record unloads. When a hauler empties a hold, right-click
the character row and pick **Reset**. You can also pick **Set current m³…**
to calibrate to what the client shows, or **Set capacity…** per character.
The default capacity is 180,000 m³ and can be changed in Settings, where an
info button lists standard hold sizes per ship (Venture through Rorqual).

In-ship compression is tracked automatically and was verified against real
gamelogs. A `Successfully compressed X into N Compressed X` line shrinks
the estimate by the right amount: compression is 1:1 by units, and
compressed ore is about 1/100 the raw volume (ice is 1/10). By default the
app assumes you drag the compressed ore out of the ore hold right after
compressing, so the full raw volume is freed. Untick that box in Settings
if you leave compressed stacks in the hold. Lines reading
`<pilot> compressed N X using your compression services`, which the
boosting ship sees, are deliberately ignored so a compression is never
counted twice.

A `Your ore hold is full` notify line snaps that character to 100% and
alerts immediately, regardless of the estimate.

The **Recalculate** button (window and tray) rebuilds every estimate purely
from the logs: anchors are dropped and the whole lookback window (24 h) is
replayed. Compressions act as natural zero points, so this lands on
reality for a compress-and-move workflow. The tray also keeps
**Reset all holds to 0** for after a full unload.

Restarts never double-count. Only your reset or calibration anchor (a
timestamp plus m³) is saved to disk; every launch rebuilds the estimate by
replaying log events newer than the anchor, so closing and reopening the
app always lands on the same number.

Each actively mining character's row shows a time-to-full estimate such as
"⏳ 2h 14m", based on that character's own mining rate over the last 10
minutes. Skills, drones, ship bonuses, and boosts are all reflected
because the rate is measured from the character's real cycles. The status
line and tray tooltip show which hold fills first fleet-wide. A pilot with
no mining cycle for 5+ minutes shows no ETA.

## Where the fill percentage is shown

- Each character row in the window: the percent chip, the colored bar, and
  the ~m³ / capacity numbers.
- The tray icon: a donut gauge of the fullest character, with the number
  in the middle. Hover it for a per-character list with ETAs.
- The taskbar button: the same live gauge is used as the window icon, and
  the window title reads "Ore Hold Watcher - 64%" (fullest character), so
  the number shows in the taskbar hover and in Alt-Tab.
- Alerts: every digest lists all characters with percent, volume, and ETA.

Colors everywhere mean the same thing: green below 75%, amber 75 to 90%,
red above 90%.

## Alerts

Alerts are fleet digests: one notification listing every character
(`Name - ~X / Y m³ (Z%) · full in 1h 32m`, fullest first), sent when any
character crosses the threshold. They are rate-limited to at most one per
"Min. time between alerts" (default 5 minutes, 0 = every crossing). An
alert suppressed by the limit sends itself the moment the window reopens.
Alerts fire once per crossing and re-arm after a reset, a compression, or
when the fill drops 5% below the threshold. **Send test alert** sends the
real current fleet state through every enabled method.

An idle alert can also be enabled on the Alerts tab (on by default): if a
pilot who was mining stops receiving ore ticks for X minutes (5 by
default), you get one alert such as "⏸ Edgar Hendar - no ore ticks for 6
min" through the same methods below. It re-arms when that pilot mines
again, and it never fires at startup for pilots who already stopped before
the app launched. Use it to catch depleted belts, returned drones, or a
client that got bumped or disconnected.

Each method is a checkbox on the Alerts tab, saved in `settings.json`:

- **Pop-up**: native Windows toast (tray balloon as fallback). Obeys Focus
  Assist, lands in the Action Center history.
- **Overlay banner**: a red always-on-top box drawn by the app at the top
  right of your screen. Ignores Focus Assist, shows over
  borderless-windowed games, dismisses on click. Use this if you tend to
  miss toasts while playing.
- **Sound**: the built-in Windows exclamation ding.
- **Webhook**: HTTP POST to any URL. Discord webhook URLs are detected
  automatically and get an `@everyone` embed with one color-coded line per
  character (see "Creating a Discord webhook" below). Any other endpoint
  receives JSON: `title`, `message`, and a `characters` array with
  `est_m3`, `capacity_m3`, `pct`, and `eta_min` per character.
- **Phone push via ntfy.sh**: install the free ntfy app, subscribe to a
  topic name of your choosing (treat it like a password), and enter the
  same topic in Settings. No account needed. For true SMS you would need a
  paid gateway such as Twilio or your carrier's email-to-SMS bridge; ntfy
  is the free substitute.

### Creating a Discord webhook

You need a text channel you control on any server. Webhooks cannot post to
DMs. If you don't have a server yet, make a free private one first: click
the `+` at the bottom of Discord's server list, pick "Create My Own", then
"For me and my friends", give it a name, done. That takes about 20
seconds and nobody else can see it.

Then, on the desktop app or in a browser (the mobile app can't manage
webhooks):

1. Hover the channel you want alerts in and click the gear (Edit Channel).
2. Open the "Integrations" tab.
3. Click "Webhooks", then "New Webhook" (or "Create Webhook" if the list
   is empty). Discord creates one with a random name.
4. Click the new webhook to expand it. Name it something like
   `Ore Watcher` (that name appears as the message author), and confirm
   the channel dropdown points where you want.
5. Click "Copy Webhook URL", then "Save Changes" if the bar appears.
6. In this app: Settings, Alerts tab, tick "Webhook", paste the URL, and
   click "Send test alert". The fleet digest should land in the channel
   within a second or two.

Two things to know. First, the "Discord ping" dropdown in Settings
controls who gets pinged: `@everyone` (default), a specific target, or no
ping at all (the embed still posts). For a specific person, enable
Developer Mode in Discord (User Settings, Advanced), right-click the user,
Copy User ID, and paste the number into the "Ping target" field; `@here`
or a role mention like `<@&roleID>` also work there. Second, treat the URL
like a password: anyone who has it can post to your channel, no login
needed. If it leaks, delete or regenerate the webhook on the same
Integrations page and paste the new URL into Settings.

## Downtime auto-close (off by default, NOT yet tested in the wild)

**Warning: this feature has not been tested against real running EVE
clients.** The scheduling logic passes unit tests, but the actual
force-close has only been exercised with mocked processes. Try it on a day
when losing an unsaved client state wouldn't hurt, and confirm the kill
works on your setup before relying on it.

On the Downtime tab, tick "Force-close all EVE clients before daily
downtime" and set the lead time (default 5 minutes). That many minutes
before the 11:00 UTC cluster shutdown, the app runs
`taskkill /F /T /IM exefile.exe`, the same command the community has long
used in a Windows scheduled task for this purpose (`/T` also kills child
processes). Process names are configurable via `eve_process_names` in
settings.json. Afterward it sends an alert saying how many clients it
closed. It fires once per day and only while the app is
running. Force-kill skips the graceful logout, so ships get EVE's normal
emergency-warp handling, which is the point of doing it before shutdown.

## Auto-update

Once the app is published on GitHub with the release workflow, the built
exe can update itself. Set "GitHub repo" on the Settings General tab to
your `owner/repo` and leave "Check GitHub for app updates" ticked. The app
checks the latest release 20 seconds after launch and daily after that;
the tray menu also has "Check for updates" for an on-demand check.

When a newer version exists you get a prompt showing both versions. Accept
and the app downloads the new exe next to the current one, writes a small
swap script, exits, and the script replaces the exe and relaunches it.
Your settings and state are untouched (they live beside the exe and are
not part of the download). Decline and it won't nag again about that
version until the next restart.

Version comparison uses the version stamped into the exe by
`release.yml`, so it only works for exes built from a tag. Running from
source? The updater stays out of the way: update with `git pull`.

## CI and releases (GitHub)

Push this folder to a GitHub repo and two workflows take over:

- `ci.yml` runs the engine tests on Linux for every push and PR, then
  builds the Windows exe with Nuitka and uploads it as a build artifact.
- `release.yml` triggers on a version tag, builds a version-stamped exe,
  and publishes a GitHub Release with `OreHoldWatcher.exe` attached:

      git tag v1.0.0
      git push origin v1.0.0

Both are non-interactive. The first CI build is slow; later ones reuse the
Nuitka compilation cache.

## Files it writes

Config lives beside the exe (or beside `app.py` when running from source).
The old `%APPDATA%\OreHoldWatcher\` location is still checked on every
startup: anything found there and missing here is copied over, and if the
exe's folder is not writable the app keeps using APPDATA. All of these are
in `.gitignore`:

- `settings.json`: log folder, threshold, capacities, alert methods,
  downtime options, and `mining_patterns` (custom regexes with named
  groups `qty` and `ore`, in case CCP changes the log wording).
- `state.json`: per-character anchors and capacities.
- `ores_override.json`: optional; add `{"Some Ore Name": 5.0}` entries in
  m³ per unit if the app reports an unknown ore.
- `debug.log`: only when debug logging is enabled.

## Debugging

Tick "Debug logging" in Settings to write `debug.log`: the resolved
config and log folders, every log file picked up with its character name,
compressions, unknown-ore warnings, each settings save, every mining cycle,
and any `(mining)` lines the parser failed to match. When unticked (the
default) no log file is written at all. The window's status line always
shows the watched folder plus live file, line, and event counters.

The Gamelogs folder is auto-detected for the active user via the Windows
known-folder API (which follows OneDrive-redirected Documents), then the
`%OneDrive%` environment variable, then plain `~\Documents`. Set it in
Settings only if yours lives somewhere else entirely.

## EULA and third-party policy notes

CCP's third-party policies prohibit botting, modifying the game client or
its UI, reverse engineering, cache scraping, input broadcasting, and tools
that acquire items or currency at an accelerated rate. This app does none
of those things:

- It reads only the plain-text gamelog files the EVE client itself writes
  to `Documents\EVE\logs\Gamelogs` when you enable "Log game messages to a
  file". That is a supported client feature, not cache scraping or memory
  reading. Community tools have read these logs (and chat logs, for intel)
  for many years.
- It never touches the EVE process, injects nothing, overlays nothing into
  the game, and modifies no game files. CCP has stated they "only care
  about what is going on in the EVE process"; this app stays entirely
  outside it.
- It sends no input to the game and automates no gameplay. It counts and
  notifies; a human still performs every action in the client. Running
  multiple clients is itself allowed by CCP, and this app does no input
  broadcasting between them.
- The optional downtime feature closes client processes, the same effect
  as closing the windows yourself. It never plays the game.
- It is free, open source, and talks to no CCP service (no ESI, no login).

One caveat, stated plainly: CCP's published position is that third-party
software is tolerated at the player's own risk, not pre-approved. Nothing
here is an official CCP endorsement, and this section is a good-faith
policy reading, not legal advice. If CCP ever changes its stance on
reading gamelogs, that change wins.

## License

MIT - see [LICENSE](LICENSE) for details. The license covers this app's
code only; EVE Online and all CCP property remain CCP's, per the notice
below.

## CCP hf. copyright notice

EVE Online and the EVE logo are the registered trademarks of CCP hf. All
rights are reserved worldwide. All other trademarks are the property of
their respective owners. EVE Online, the EVE logo, EVE and all associated
logos and designs are the intellectual property of CCP hf. All artwork,
screenshots, characters, vehicles, storylines, world facts or other
recognizable features of the intellectual property relating to these
trademarks are likewise the intellectual property of CCP hf. CCP hf. has
granted permission to use EVE Online and all associated logos and designs
for promotional and information purposes but does not endorse, and is not
in any way affiliated with, Ore Hold Watcher. CCP is in no way responsible
for the content on or functioning of this application, nor can it be
liable for any damage arising from the use of this application.

## Troubleshooting

- Nothing shows up: check Settings for the Gamelogs folder, confirm you
  have mined since starting the app (it reads logs from the last 24 h),
  and make sure EVE's "Log game messages to a file" option is on
  (Esc > Chat and logging settings).
- Numbers drift from reality: logs cannot see jettisons or fleet-hangar
  transfers of raw ore. Use **Set current m³…** to re-sync, **Reset** when
  a hold is emptied, or **Recalculate** to rebuild from the logs.
- Unknown ore toast: add that ore to `ores_override.json` and it counts
  from then on.
