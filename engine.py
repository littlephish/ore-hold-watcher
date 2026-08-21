# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LittlePhish
"""Log-watching / ore-hold-estimation engine for Ore Hold Watcher.

Pure Python (no Qt) so it can be unit-tested headless. The GUI drives it by
calling Engine.poll() periodically and consuming the returned events.

How it works
------------
EVE Online writes one gamelog file per client session to
  %USERPROFILE%/Documents/EVE/logs/Gamelogs/YYYYMMDD_HHMMSS(_charid).txt
Each file starts with a header block containing "Listener: <Character Name>".
We tail every file modified within `lookback_hours`, parse mining result
lines, convert mined units -> m3 via the ore table, and accumulate an
estimated ore hold fill per character. The estimate is reset manually by the
user (when they unload) or calibrated to a known m3 value.
"""

from __future__ import annotations

import calendar
import json
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import ores

log = logging.getLogger("orewatcher.engine")

# ---------------------------------------------------------------------------
# Line parsing
# ---------------------------------------------------------------------------

# [ 2026.07.15 12:34:56 ] (mining) message...
LINE_RE = re.compile(
    r"^\[\s*(?P<ts>\d{4}\.\d{2}\.\d{2}\s+\d{2}:\d{2}:\d{2})\s*\]\s*"
    r"\((?P<channel>[^)]+)\)\s*(?P<msg>.*)$"
)

LISTENER_RE = re.compile(r"Listener:\s*(?P<name>.+?)\s*$")

TAG_RE = re.compile(r"<[^>]+>")  # strip <color=...>, <b>, etc.
GRADE_SUFFIX_RE = re.compile(r"\s+[ivx]+-grade$", re.IGNORECASE)

# Number like 1,244 or 1 244 or 1'244 or 1244
_NUM = r"[\d][\d,.  '\s]*"

# Default mining patterns; overridable via settings.json ("mining_patterns").
# Tried in order against the tag-stripped message of (mining)/(notify)/(info)
# channel lines. Must expose named groups 'qty' and 'ore'.
DEFAULT_MINING_PATTERNS = [
    # "You have successfully mined 1,244 units of Veldspar" (and variants)
    rf"You\s+(?:have\s+)?(?:successfully\s+)?min(?:ed|e)\s+(?P<qty>{_NUM})\s+units?\s+of\s+(?P<ore>.+?)\s*[.!]*\s*$",
    # "Your mining laser/harvester ... extracted 1,244 units of Blue Ice"
    rf"(?:extract(?:ed|s)|harvest(?:ed|s)|acquir(?:ed|es))\s+(?P<qty>{_NUM})\s+units?\s+of\s+(?P<ore>.+?)\s*[.!]*\s*$",
    # "1,244 units of Veldspar was mined / transferred to your ore hold"
    rf"^(?P<qty>{_NUM})\s+units?\s+of\s+(?P<ore>.+?)\s+(?:was|were|has been|have been)\s+(?:mined|extracted|transferred|deposited)",
]

MINING_CHANNELS = {"mining", "notify", "info"}

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

# "[ ts ] (None) Jumping from AK-LNZ to NV-ZHM" - verified in real gamelogs.
# Note the channel is "None", not a mining channel, so this is matched before
# the MINING_CHANNELS filter. A gate jump always changes system, and asteroids
# are grid-local, so it always invalidates a tracked rock.
SYSTEM_CHANGE_RE = re.compile(
    r"Jumping from\s+(?P<from_sys>.+?)\s+to\s+(?P<to_sys>.+?)\s*$",
    re.IGNORECASE,
)

# "(notify) Successfully compressed Glistening Zeolites into 794 Compressed
#  Glistening Zeolites."  Compression is 1:1 by units (verified against real
# logs), so N compressed units consumed N raw units; the hold shrinks by
# N * (raw_vol - compressed_vol).
COMPRESS_RE = re.compile(
    rf"Successfully compressed\s+(?P<ore>.+?)\s+into\s+(?P<qty>{_NUM})\s+"
    rf"(?:units?\s+of\s+)?(?:Batch\s+)?Compressed\s+",
    re.IGNORECASE,
)

def now_ts() -> str:
    """Current time in EVE log-timestamp format (EVE time == UTC).
    The zero-padded format compares correctly as a plain string."""
    return time.strftime("%Y.%m.%d %H:%M:%S", time.gmtime())


def ts_to_epoch(ts: str) -> float:
    """Log timestamp ('2026.07.16 11:15:33', UTC) -> unix epoch."""
    try:
        return calendar.timegm(time.strptime(ts, "%Y.%m.%d %H:%M:%S"))
    except ValueError:
        return 0.0


RATE_WINDOW_S = 600   # mining rate = volume over the last 10 minutes
RATE_IDLE_S = 300     # no cycle for 5 min -> treat as not mining (no ETA)

# Ticks arrive in BURSTS, not one at a time: several lasers on one ship land
# within a few seconds of each other, and a residue line lands within 2 s of
# the tick it belongs to. Anything closer together than this is the same
# cycle, so counting raw lines badly overstates how much evidence we have.
# Measured against real logs: 3-laser bursts land inside ~3 s, cycles repeat
# every 30-60 s.
BURST_GAP_S = 10.0

# A rock rate needs one full cycle between bursts before it means anything -
# the residue share varies by crystal and ship, so it is measured, never
# assumed. The span floor only rejects gaps too short to be a real cycle;
# two lasers firing 2 s apart must never read as 780 units/min.
ROCK_WARMUP_BURSTS = 2
ROCK_WARMUP_S = 30.0


HOLD_FULL_MARKERS = (
    "ore hold is full",
    "mining hold is full",
    "cargo hold is full",
    "is full and cannot accept",
)

# lines that look like mining but must NOT be counted
EXCLUDE_MARKERS = ("residue", "wasted", "lost")

# "(notify) Mining Drone I deactivates as it finds the resource it was
# harvesting a pale shadow of its former glory."  Verified in real logs:
# a mining drone auto-returned because its asteroid depleted.
DRONE_STOP_RE = re.compile(
    r"Mining Drone.*deactivates as it finds the resource", re.IGNORECASE)

# --- combat (being attacked) ---
# Incoming damage after tag-strip: "287 from Attacker - Smashes"
COMBAT_DMG_RE = re.compile(
    rf"^(?P<dmg>{_NUM})\s+from\s+(?P<who>.+?)(?:\s+-\s+.*)?$")
# EWAR aimed at you: "Warp scramble attempt from Attacker to you"
COMBAT_EWAR_RE = re.compile(
    r"^(?P<kind>Warp (?:scramble|disruption) attempt|"
    r"Remote sensor dampener|Sensor jam attempt)\s+from\s+(?P<who>.+?)\s+"
    r"(?:to you|against you)", re.IGNORECASE)
# Player attackers render as "Name[CORP](Ship)"; NPCs are plain names.
PLAYER_ATTACKER_RE = re.compile(r"\[[^\]]{1,10}\]|\([^)]+\)\s*$")


def parse_qty(raw: str) -> int:
    digits = re.sub(r"[^\d]", "", raw)
    return int(digits) if digits else 0


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@dataclass
class MiningEvent:
    character: str
    qty: int
    ore: str
    m3: float
    ts: str


@dataclass
class HoldFullEvent:
    character: str
    ts: str


@dataclass
class UnknownOreEvent:
    character: str
    ore: str
    qty: int


@dataclass
class DroneStopEvent:
    character: str
    ts: str


@dataclass
class CombatEvent:
    character: str
    attacker: str
    kind: str          # "damage" or the ewar kind
    is_player: bool    # True when the attacker looks like a capsuleer
    ts: str


@dataclass
class CompressionEvent:
    character: str
    qty: int          # compressed units produced == raw units consumed (1:1)
    ore: str          # raw ore name
    delta_m3: float   # negative: how much the hold shrank
    ts: str


@dataclass
class ResidueEvent:
    character: str
    qty: int          # units removed from the asteroid, NOT added to the hold
    ore: str          # inferred from the preceding mining tick
    ts: str


@dataclass
class SystemChangeEvent:
    character: str
    to_system: str
    ts: str


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


# ---------------------------------------------------------------------------
# Ore volume lookup
# ---------------------------------------------------------------------------

class OreTable:
    def __init__(self, override_path: Path | None = None,
                 sde_paths: list[Path] | None = None):
        self.base = {k.lower(): v for k, v in ores.ORE_VOLUMES.items()}
        self.compressed = {k.lower(): v for k, v in ores.COMPRESSED_VOLUMES.items()}
        self.overrides: dict[str, float] = {}
        self.sde: dict[str, float] = {}
        if override_path and override_path.exists():
            try:
                data = json.loads(override_path.read_text(encoding="utf-8"))
                self.overrides = {str(k).lower(): float(v) for k, v in data.items()}
            except Exception:
                pass  # a broken override file should never kill the app
        self.load_sde(sde_paths or [])

    def load_sde(self, paths: list[Path] | None) -> None:
        """Load SDE catalogs in priority order, ignoring broken files."""
        self.sde = {}
        for path in paths or []:
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                for name, volume in data.items():
                    self.sde.setdefault(str(name).lower(), float(volume))
            except (OSError, TypeError, ValueError):
                continue

    def unit_volume(self, name: str) -> float | None:
        n = " ".join(name.split()).strip().strip(".*").lower()
        if not n:
            return None
        if n in self.overrides:
            return self.overrides[n]
        if n in self.sde:
            return self.sde[n]
        if n in self.base:
            return self.base[n]
        graded = GRADE_SUFFIX_RE.sub("", n)
        if graded != n:
            n = graded
            if n in self.overrides:
                return self.overrides[n]
            if n in self.sde:
                return self.sde[n]
            if n in self.base:
                return self.base[n]
        for prefix in ("batch compressed ", "compressed "):
            if n.startswith(prefix):
                rest = n[len(prefix):]
                vol = self._suffix_lookup(rest, self.compressed)
                if vol is not None:
                    return vol
                base = self._suffix_lookup(rest, self.base)
                if base is not None:
                    return base / 100.0
                return None
        volume = self._suffix_lookup(n, self.sde)
        return volume if volume is not None else self._suffix_lookup(n, self.base)

    @staticmethod
    def _suffix_lookup(n: str, table: dict[str, float]) -> float | None:
        if n in table:
            return table[n]
        best = None
        for key, vol in table.items():
            if n.endswith(" " + key) and (best is None or len(key) > best[0]):
                best = (len(key), vol)
        return best[1] if best else None


# ---------------------------------------------------------------------------
# Per-file tailer
# ---------------------------------------------------------------------------

class LogFile:
    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.encoding: str | None = None
        self.character: str | None = None
        self.remainder = ""
        self.header_scanned = False

    def _detect_encoding(self, head: bytes) -> str:
        if head.startswith(b"\xff\xfe"):
            return "utf-16-le"
        if head.startswith(b"\xfe\xff"):
            return "utf-16-be"
        if head.startswith(b"\xef\xbb\xbf"):
            return "utf-8-sig"
        return "utf-8"

    def read_new_lines(self) -> list[str]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.offset:  # rotated/truncated
            self.offset = 0
            self.remainder = ""
        if size == self.offset:
            return []
        try:
            with open(self.path, "rb") as f:
                if self.encoding is None:
                    head = f.read(4)
                    self.encoding = self._detect_encoding(head)
                f.seek(self.offset)
                chunk = f.read(size - self.offset)
                self.offset = size
        except OSError:
            return []
        text = self.remainder + chunk.decode(self.encoding, errors="ignore")
        lines = text.split("\n")
        self.remainder = lines.pop()  # possibly-partial last line
        out = [ln.rstrip("\r").lstrip("﻿") for ln in lines]
        if not self.header_scanned:
            for ln in out:
                m = LISTENER_RE.search(ln)
                if m:
                    self.character = m.group("name").strip()
                    self.header_scanned = True
                    log.info("listener '%s' -> %s (%s)",
                             self.character, self.path.name, self.encoding)
                    break
            # give up on header after ~40 lines; fall back to filename
            if not self.header_scanned and self.offset > 4096:
                self.header_scanned = True
        return out


# ---------------------------------------------------------------------------
# Character state
# ---------------------------------------------------------------------------

@dataclass
class CharacterState:
    name: str
    est_m3: float = 0.0
    capacity: float = 180000.0
    last_event: float = 0.0          # wall-clock of last mining event
    notified: bool = False           # threshold toast already sent
    unknown_ores: dict = field(default_factory=dict)
    # Anchor: the point in (EVE/UTC log-)time the estimate is measured from.
    # est_m3 is always anchor_m3 + volume of log events NEWER than anchor_ts.
    # Only the anchor is persisted; on startup the estimate is recalculated
    # by replaying the logs, so restarts never double-count.
    anchor_ts: str = ""              # "YYYY.MM.DD HH:MM:SS" (log format, UTC)
    anchor_m3: float = 0.0
    # rolling (epoch, m3) mining events for rate/ETA; not persisted
    rate_events: deque = field(default_factory=deque)
    # idle alert arming: starts True (disarmed) so startup replay of old
    # logs can't fire it; a recent live mining tick arms it (False), going
    # idle fires once and disarms again until mining resumes
    idle_notified: bool = True
    # scanned-rock countdown (see TargetRock); persisted via save_state
    target: "TargetRock | None" = None
    # rolling (epoch, ore, units) for EVERY ore this pilot ticked - mined AND
    # residue. Rate history is a property of the ship, not of the rock, so it
    # is kept per ore and independent of any scan anchor: that is what lets a
    # freshly pasted scan have an ETA immediately instead of measuring from
    # scratch. Not persisted; the startup replay refills it.
    ore_ticks: deque = field(default_factory=deque)

    def note_tick(self, epoch: float, ore: str, units: int):
        """Record one mining/residue tick for the rolling rate window."""
        if not epoch:
            return
        self.ore_ticks.append((epoch, ore, int(units)))
        while (self.ore_ticks and
               epoch - self.ore_ticks[0][0] > RATE_WINDOW_S):
            self.ore_ticks.popleft()

    def ore_tick_times(self, ore: str) -> list[tuple[float, int]]:
        """[(epoch, units)] for one ore, oldest first."""
        return [(ep, u) for ep, o, u in self.ore_ticks if o == ore]

    def mining_ore(self, now_epoch: float | None = None) -> str | None:
        """The ore this pilot is ticking RIGHT NOW, straight from the log.

        Every mining line names its ore ("You mined 13 units of Zeolites"), so
        this is read, never inferred.
        """
        if not self.ore_ticks:
            return None
        now_epoch = now_epoch if now_epoch is not None else time.time()
        if now_epoch - self.ore_ticks[-1][0] > RATE_IDLE_S:
            return None
        return self.ore_ticks[-1][1]

    def rock_remaining(self) -> int | None:
        """Units left in the scanned rock, or None when no rock is targeted."""
        if not self.target:
            return None
        return max(0, self.target.scan_units - self.target.depleted_units)

    def rock_depletion_rate(self, now_epoch: float | None = None) -> float:
        """Units per minute coming off the rock (mined + residue); 0 when idle
        or still warming up.

        Measured over CYCLES, not log lines, and the first burst's units are
        deliberately left out of the numerator: they were removed before the
        window opened. Counting them would inflate the rate by
        bursts/(bursts-1) - a 50% overshoot at three bursts - which reads as a
        countdown that is confidently too short.
        """
        if not self.target:
            return 0.0
        ticks = self.ore_tick_times(self.target.ore)
        if not ticks:
            return 0.0
        now_epoch = now_epoch if now_epoch is not None else time.time()
        if now_epoch - ticks[-1][0] > RATE_IDLE_S:
            return 0.0
        bursts = tick_bursts(ticks)
        span = bursts[-1][0] - bursts[0][0]
        # Below the warm-up threshold the rate is noise, not a number.
        if len(bursts) < ROCK_WARMUP_BURSTS or span < ROCK_WARMUP_S:
            return 0.0
        return sum(u for _, u in bursts[1:]) / (span / 60.0)

    def rock_status(self, now_epoch: float | None = None) -> str:
        """Why there is (or isn't) a rock ETA: 'ready', 'warmup', or 'idle'.

        The UI needs to tell these apart. A bare "-" during warm-up reads as a
        broken feature; saying so costs nothing and buys trust.
        """
        if not self.target:
            return "idle"
        now_epoch = now_epoch if now_epoch is not None else time.time()
        ticks = self.ore_tick_times(self.target.ore)
        if not ticks or now_epoch - ticks[-1][0] > RATE_IDLE_S:
            # Mining something else is not the same as not mining: a tracked
            # rock nobody is touching would otherwise sit at a frozen count
            # forever, looking live. Name it so the UI can say so.
            other = self.mining_ore(now_epoch)
            return "mismatch" if other else "idle"
        bursts = tick_bursts(ticks)
        span = bursts[-1][0] - bursts[0][0]
        if len(bursts) < ROCK_WARMUP_BURSTS or span < ROCK_WARMUP_S:
            return "warmup"
        return "ready"

    def rock_active(self, now_epoch: float | None = None) -> bool:
        """Is this rock demonstrably being mined RIGHT NOW?

        True only when a tick naming this rock's own ore landed within
        RATE_IDLE_S. Every mining line names its ore, so this is evidence read
        from the log - not an assumption that the ship never moved. A pilot
        busily mining a DIFFERENT ore is not on this rock and reads False.
        """
        if not self.target:
            return False
        ticks = self.ore_tick_times(self.target.ore)
        if not ticks:
            return False
        now_epoch = now_epoch if now_epoch is not None else time.time()
        return now_epoch - ticks[-1][0] <= RATE_IDLE_S

    def rock_eta_s(self, now_epoch: float | None = None) -> float | None:
        """Seconds until the scanned rock is dry; None when unknown."""
        remaining = self.rock_remaining()
        if not remaining:
            return None
        rate = self.rock_depletion_rate(now_epoch)
        if rate <= 0:
            return None
        return remaining / rate * 60.0

    def mining_rate_m3_min(self, now_epoch: float | None = None) -> float:
        """Current mining speed in m3/min over the rolling window;
        0 when idle for RATE_IDLE_S or no data."""
        if not self.rate_events:
            return 0.0
        now_epoch = now_epoch if now_epoch is not None else time.time()
        newest = self.rate_events[-1][0]
        if now_epoch - newest > RATE_IDLE_S:
            return 0.0
        oldest = self.rate_events[0][0]
        span = max(60.0, newest - oldest)   # floor: one cycle ≠ infinite rate
        total = sum(m for _, m in self.rate_events)
        return total / (span / 60.0)

    def eta_full_s(self, now_epoch: float | None = None) -> float | None:
        """Seconds until this hold hits capacity at the current rate;
        None when not actively mining, 0 when already full."""
        if self.est_m3 >= self.capacity:
            return 0.0
        rate = self.mining_rate_m3_min(now_epoch)
        if rate <= 0:
            return None
        return (self.capacity - self.est_m3) / rate * 60.0

    @property
    def pct(self) -> float:
        return 100.0 * self.est_m3 / self.capacity if self.capacity else 0.0


def tick_bursts(ticks: list[tuple[float, int]]) -> list[tuple[float, int]]:
    """[(epoch, units)] -> one (burst_start, units_in_burst) entry per cycle.

    Collapses a multi-laser volley (and the residue line trailing it) into the
    single mining cycle it really is, so a rate is measured per cycle rather
    than per log line.
    """
    bursts: list[tuple[float, int]] = []
    for ep, units in ticks:
        if bursts and ep - bursts[-1][0] <= BURST_GAP_S:
            start, total = bursts[-1]
            bursts[-1] = (start, total + units)
        else:
            bursts.append((ep, units))
    return bursts


# ---------------------------------------------------------------------------
# Scanned-rock countdown (gauge inner ring)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RockCountdown:
    """The tracked rock that will run dry first, across all characters."""
    character: str
    ore: str
    remaining: int
    fraction: float          # units left / units at scan: 1.0 -> 0.0
    eta_s: float | None      # None while warming up or idle


def fastest_rock(chars, now_epoch: float | None = None) -> "RockCountdown | None":
    """Pick the rock the fleet will exhaust soonest; None when none tracked.

    Ranked by ETA (soonest first) because that is the thing a miner has to
    react to. Characters with no ETA yet (warm-up or idle) sort last, broken
    by how little of their rock is left.

    `fraction` is deliberately units-based, not time-based: it is defined the
    instant a scan is pasted (no warm-up dead zone), and it only ever moves
    down, because depleted_units only goes up. A time-based ratio would jump
    backwards every time the rate estimate wobbled. At a steady rate the two
    are the same curve anyway - units left IS time left.
    """
    best = None
    best_key = None
    for c in chars:
        remaining = c.rock_remaining()
        if not remaining:            # no rock targeted, or already dry
            continue
        fraction = min(1.0, remaining / max(1, c.target.scan_units))
        eta_s = c.rock_eta_s(now_epoch)
        key = (eta_s if eta_s is not None else float("inf"), fraction)
        if best_key is None or key < best_key:
            best_key = key
            best = RockCountdown(character=c.name, ore=c.target.ore,
                                 remaining=remaining, fraction=fraction,
                                 eta_s=eta_s)
    return best


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class Engine:
    def __init__(self, log_dir: Path, state_path: Path,
                 ore_override_path: Path | None = None,
                 mining_patterns: list[str] | None = None,
                 lookback_hours: float = 24.0,
                 default_capacity: float = 180000.0,
                 compressed_leaves_hold: bool = True,
                 combat_enabled: bool = True,
                 ledger_path: Path | None = None,
                 ledger_enabled: bool = False,
                 sde_paths: list[Path] | None = None):
        self.log_dir = Path(log_dir)
        self.state_path = Path(state_path)
        self.lookback_hours = lookback_hours
        self.default_capacity = default_capacity
        # True = user moves compressed ore out of the ore hold (fleet hangar,
        # etc.) right after compressing, so a compression frees the FULL raw
        # volume. False = compressed stacks stay in the ore hold at their
        # (tiny) compressed volume.
        self.compressed_leaves_hold = compressed_leaves_hold
        # when False, (combat) lines are skipped at the parser level -
        # no combat scanning happens at all
        self.combat_enabled = combat_enabled
        self.drone_enabled = False   # scan (notify) for mining-drone stops
        # daily mining ledger: per UTC day, per character, units by ore.
        # "marks" holds a per-character high-water log timestamp so replays
        # (restart, Recalculate) never double-count into the ledger.
        self.ledger_enabled = ledger_enabled
        self.ledger_path = Path(ledger_path) if ledger_path else None
        # days:   day -> char -> ore -> units mined (ground truth)
        # prices: day -> ore -> Jita buy price FROZEN for that day (a value
        #         snapshot; quantities stay exact, only the ISK basis is
        #         stored so past days keep their worth-when-mined)
        # marks:  char -> high-water log ts (replay-safe accrual)
        # activity: day -> char -> state -> seconds spent in that state
        #         (mining / idle / full / offline). Forward-accumulated from
        #         live ticks by the GUI; not derived from logs on replay.
        self.ledger = {"marks": {}, "days": {}, "prices": {}, "activity": {}}
        self._load_ledger()
        self._sde_paths = list(sde_paths or [])
        self.table = OreTable(ore_override_path, self._sde_paths)
        pats = mining_patterns or DEFAULT_MINING_PATTERNS
        self.patterns = [re.compile(p, re.IGNORECASE) for p in pats]
        self.files: dict[str, LogFile] = {}
        self.chars: dict[str, CharacterState] = {}
        # character -> (ts, ore) of the most recent mining tick, used to give
        # the ore-less residue line an ore name
        self._last_tick: dict[str, tuple[str, str]] = {}
        self._last_scan = 0.0
        self._warned_missing_dir = False
        self._unmatched_logged = 0
        self.stats = {"lines": 0, "mining_events": 0, "compress_events": 0,
                      "unmatched_mining": 0, "last_event_wall": 0.0}
        log.info("engine start: log_dir=%s exists=%s lookback=%.0fh "
                 "compressed_leaves_hold=%s patterns=%d",
                 self.log_dir, self.log_dir.is_dir(), lookback_hours,
                 compressed_leaves_hold, len(self.patterns))
        self.load_state()

    def reload_sde(self, paths: list[Path]) -> None:
        """Reload SDE volumes after a catalog update."""
        self._sde_paths = list(paths)
        self.table.load_sde(self._sde_paths)

    # -- persistence --------------------------------------------------------
    def load_state(self):
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return
        for name, d in data.get("characters", {}).items():
            anchor_m3 = float(d.get("anchor_m3", 0.0))
            self.chars[name] = CharacterState(
                name=name,
                est_m3=anchor_m3,  # replaying the logs adds post-anchor events
                capacity=float(d.get("capacity", self.default_capacity)),
                last_event=float(d.get("last_event", 0.0)),
                notified=bool(d.get("notified", False)),
                anchor_ts=str(d.get("anchor_ts", "")),
                anchor_m3=anchor_m3,
            )
            td = d.get("target")
            if isinstance(td, dict) and td.get("ore"):
                self.chars[name].target = TargetRock(
                    ore=str(td["ore"]),
                    scan_units=int(td.get("scan_units", 0)),
                    scan_ts=str(td.get("scan_ts", "")),
                    distance_m=float(td.get("distance_m", 0.0)),
                    depleted_units=int(td.get("depleted_units", 0)),
                )

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
            self.state_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    # -- ledger ---------------------------------------------------------------
    def _load_ledger(self):
        if not (self.ledger_path and self.ledger_path.exists()):
            return
        try:
            data = json.loads(self.ledger_path.read_text(encoding="utf-8"))
            if isinstance(data.get("days"), dict):
                self.ledger = {"marks": dict(data.get("marks", {})),
                               "days": data["days"],
                               "prices": dict(data.get("prices", {})),
                               "activity": dict(data.get("activity", {}))}
        except Exception as e:
            log.warning("ledger load failed: %s", e)

    def snapshot_prices(self, day: str, price_map: dict) -> bool:
        """Freeze the given day's ISK price basis. Overwrites only the day
        passed (callers pass today), so past days stay frozen. Returns True
        if anything changed."""
        if not price_map:
            return False
        clean = {o: float(p) for o, p in price_map.items() if p}
        if self.ledger["prices"].get(day) == clean:
            return False
        self.ledger["prices"][day] = clean
        return True

    def save_ledger(self):
        if not self.ledger_path:
            return
        days = self.ledger["days"]
        activity = self.ledger.setdefault("activity", {})
        # keep window spans BOTH mined-ore days and activity days: a day can
        # have logged time-in-state without a single mining event (all idle)
        all_days = set(days) | set(activity)
        keep = set(sorted(all_days)[-400:])   # ~13 months of history
        for old in [d for d in days if d not in keep]:
            days.pop(old, None)
        for old in [d for d in self.ledger["prices"] if d not in keep]:
            self.ledger["prices"].pop(old, None)
        for old in [d for d in activity if d not in keep]:
            activity.pop(old, None)
        try:
            self.ledger_path.write_text(
                json.dumps(self.ledger, indent=1), encoding="utf-8")
        except OSError as e:
            log.warning("ledger save failed: %s", e)

    def _ledger_add(self, ev: MiningEvent) -> bool:
        mark = self.ledger["marks"].get(ev.character, "")
        if mark and ev.ts <= mark:
            return False                # already counted (replay)
        day = ev.ts[:10]                # "YYYY.MM.DD" (UTC == EVE time)
        per_char = self.ledger["days"].setdefault(day, {})
        ores_d = per_char.setdefault(ev.character, {})
        ores_d[ev.ore] = ores_d.get(ev.ore, 0) + ev.qty
        self.ledger["marks"][ev.character] = ev.ts
        return True

    def activity_add(self, character: str, state: str, seconds: float,
                     day: str | None = None) -> bool:
        """Add `seconds` of wall-clock time to (day, character, state).
        State is one of mining/idle/full/offline. Returns True if anything
        was recorded (so the caller can mark the ledger dirty)."""
        if not state or seconds <= 0:
            return False
        day = day or now_ts()[:10]      # "YYYY.MM.DD" (UTC == EVE time)
        per_char = self.ledger.setdefault("activity", {}).setdefault(day, {})
        states = per_char.setdefault(character, {})
        states[state] = states.get(state, 0.0) + float(seconds)
        return True

    # -- character helpers ---------------------------------------------------
    def char(self, name: str) -> CharacterState:
        if name not in self.chars:
            self.chars[name] = CharacterState(name=name, capacity=self.default_capacity)
        return self.chars[name]

    def reset(self, name: str):
        c = self.char(name)
        c.est_m3 = 0.0
        c.anchor_ts = now_ts()
        c.anchor_m3 = 0.0
        c.notified = False
        self.save_state()

    def reset_all(self):
        ts = now_ts()
        for c in self.chars.values():
            c.est_m3 = 0.0
            c.anchor_ts = ts
            c.anchor_m3 = 0.0
            c.notified = False
        self.save_state()

    def recalculate(self):
        """Rebuild every estimate from the logs alone: drop all anchors and
        replay the whole lookback window from the top of each file."""
        log.info("recalculating all characters from logs")
        for c in self.chars.values():
            c.est_m3 = 0.0
            c.anchor_ts = ""
            c.anchor_m3 = 0.0
            c.notified = False
            c.rate_events.clear()  # replay refills these; keeping them would
                                   # double-count the rate and wreck the ETA
        self.files.clear()      # forget offsets -> re-read from byte 0
        self._last_scan = 0.0   # force immediate rediscovery
        self.save_state()
        return self.poll()      # replay now so the UI updates immediately

    def calibrate(self, name: str, m3: float):
        c = self.char(name)
        c.est_m3 = max(0.0, float(m3))
        c.anchor_ts = now_ts()
        c.anchor_m3 = c.est_m3
        if c.est_m3 < c.capacity:
            c.notified = False
        self.save_state()

    # -- scanned rock ---------------------------------------------------------
    def set_target(self, character: str, ore: str, units: int,
                   distance_m: float):
        """Anchor a scanned rock to a character. Re-pasting re-anchors."""
        c = self.char(character)
        c.target = TargetRock(ore=ore, scan_units=int(units), scan_ts=now_ts(),
                              distance_m=float(distance_m))
        # NOTE: ore_ticks is deliberately NOT cleared. The pilot was already
        # mining when they pasted the scan, and how fast they chew this ore is
        # measured from that history - so the countdown starts with a real
        # rate instead of waiting a cycle to rediscover what we already knew.
        log.info("target: %s -> %s %d units @ %.0f m",
                 character, ore, units, distance_m)
        self.save_state()

    def clear_target(self, character: str):
        c = self.chars.get(character)
        if not c:
            return
        c.target = None
        self.save_state()

    def abandon_target(self, character: str, reason: str) -> bool:
        """Drop a tracked rock, saying why. No-op when nothing is tracked.

        Every path that invalidates a rock funnels through here so the reason
        is always in the log - a countdown that vanishes without explanation
        looks like a bug.
        """
        c = self.chars.get(character)
        if not c or not c.target:
            return False
        log.info("target: %s dropping %s rock (%s)",
                 character, c.target.ore, reason)
        self.clear_target(character)
        return True

    def rock_popped(self, character: str):
        """An observed pop (drone stop) outranks arithmetic - spec D4."""
        self.abandon_target(character, "popped (observed)")

    def left_system(self, character: str):
        """Abandon the tracked rock: asteroids are grid-local, and scanner
        distances are measured from the ship, so a rock scanned in the system
        you just left can never be the one you are mining now."""
        self.abandon_target(character, "left the system")

    def client_closed(self, character: str) -> bool:
        """The EVE client went away. Docking, ship swaps and belt changes all
        happen unobserved while it is shut, so whatever is mined next is not
        provably this rock - and a countdown nobody is watching keeps ticking
        down to a "dry" that never happened."""
        return self.abandon_target(character, "client closed")

    def drop_stale_targets(self, now_epoch: float | None = None) -> list[str]:
        """Startup sweep: keep only rocks the logs show still being mined.

        The target survives a restart (that is the point of persisting it),
        but surviving is conditional: after the replay, a rock with no tick of
        its own ore in the last RATE_IDLE_S is a rock the pilot has already
        walked away from. Restoring that would show a confident countdown for
        an asteroid that may not exist any more.
        """
        dropped = []
        for name, c in list(self.chars.items()):
            if c.target and not c.rock_active(now_epoch):
                if self.abandon_target(name, "not mining it at startup"):
                    dropped.append(name)
        return dropped

    def _apply_depletion(self, ev):
        """Count a mining or residue event against the character's rock.

        Gated on the rock's own scan_ts, independent of the hold anchor, so a
        calibration newer than the scan cannot silently stop depletion.
        """
        c = self.chars.get(ev.character)
        if not c:
            return
        # Rate history first, and for EVERY ore: it is how fast this ship
        # chews rock, which is true whether or not the tick matches the
        # tracked one - and knowing what else they are mining is what lets a
        # frozen countdown be reported instead of just sitting there.
        c.note_tick(ts_to_epoch(ev.ts), ev.ore, ev.qty)
        if not c.target or ev.ore != c.target.ore:
            return
        if ev.ts <= c.target.scan_ts:   # log timestamps sort lexicographically
            return
        c.target.depleted_units += ev.qty
        if c.rock_remaining() <= 0:
            self.abandon_target(ev.character, "exhausted by count")

    def set_capacity(self, name: str, m3: float):
        c = self.char(name)
        c.capacity = max(1.0, float(m3))
        self.save_state()

    def remove(self, name: str):
        self.chars.pop(name, None)
        self.save_state()

    # -- polling -------------------------------------------------------------
    def _discover(self):
        if not self.log_dir.is_dir():
            if not self._warned_missing_dir:
                self._warned_missing_dir = True
                log.warning("gamelogs folder does not exist: %s", self.log_dir)
            return
        self._warned_missing_dir = False
        cutoff = time.time() - self.lookback_hours * 3600
        try:
            entries = list(self.log_dir.iterdir())
        except OSError as e:
            log.warning("cannot list %s: %s", self.log_dir, e)
            return
        for p in entries:
            if p.suffix.lower() != ".txt":
                continue
            key = str(p)
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                self.files.pop(key, None)
                continue
            if key not in self.files:
                self.files[key] = LogFile(p)
                log.info("watching %s (mtime %s)", p.name,
                         time.strftime("%H:%M:%S", time.localtime(mtime)))

    def poll(self) -> list:
        """Read new log data; return a list of events."""
        now = time.time()
        if now - self._last_scan > 10:  # rescan dir for new files every 10 s
            self._discover()
            self._last_scan = now
        events: list = []
        dirty = False
        ledger_dirty = False
        # filenames start with the session timestamp; sorted order keeps a
        # character's events chronological so the ledger high-water mark
        # can't skip an older file ingested after a newer one
        for lf in sorted(self.files.values(), key=lambda f: f.path.name):
            lines = lf.read_new_lines()
            if not lines:
                continue
            self.stats["lines"] += len(lines)
            name = lf.character or lf.path.stem
            for ln in lines:
                ev = self._parse_line(name, ln)
                if ev is None:
                    continue
                # Rock depletion runs BEFORE the hold anchor filter below:
                # the anchor is about cargo, and a calibration newer than the
                # scan must not silently freeze the countdown. _apply_depletion
                # gates on the rock's own scan_ts instead.
                if isinstance(ev, (MiningEvent, ResidueEvent)):
                    self._apply_depletion(ev)
                elif isinstance(ev, DroneStopEvent):
                    self.rock_popped(ev.character)
                elif isinstance(ev, SystemChangeEvent):
                    self.left_system(ev.character)
                # anchor filter: log events at/before a character's last
                # reset/calibration are already baked into anchor_m3 -
                # skipping them makes startup replay idempotent.
                # CombatEvents skip this: they must not create character
                # rows (a non-mining alt getting shot is not fleet cargo)
                # CombatEvent and DroneStopEvent are transient signals, not
                # cargo - they never create rows or pass the anchor filter
                ev_ts = getattr(ev, "ts", None)
                if (ev_ts is not None and
                        not isinstance(ev, (CombatEvent, DroneStopEvent,
                                            SystemChangeEvent))):
                    c = self.char(ev.character)
                    if c.anchor_ts and ev_ts <= c.anchor_ts:
                        continue
                events.append(ev)
                self.stats["last_event_wall"] = now
                if isinstance(ev, MiningEvent):
                    self.stats["mining_events"] += 1
                    log.debug("mining: %s +%d %s = %.1f m3",
                              ev.character, ev.qty, ev.ore, ev.m3)
                    if self.ledger_enabled and self._ledger_add(ev):
                        ledger_dirty = True
                    c = self.char(ev.character)
                    c.est_m3 += ev.m3
                    c.last_event = now
                    ep = ts_to_epoch(ev.ts)
                    if ep:
                        c.rate_events.append((ep, ev.m3))
                        while (c.rate_events and
                               ep - c.rate_events[0][0] > RATE_WINDOW_S):
                            c.rate_events.popleft()
                    dirty = True
                elif isinstance(ev, HoldFullEvent):
                    c = self.char(ev.character)
                    c.est_m3 = c.capacity
                    c.last_event = now
                    dirty = True
                elif isinstance(ev, CompressionEvent):
                    self.stats["compress_events"] += 1
                    log.info("compress: %s %d %s -> delta %.1f m3",
                             ev.character, ev.qty, ev.ore, ev.delta_m3)
                    c = self.char(ev.character)
                    c.est_m3 = max(0.0, c.est_m3 + ev.delta_m3)
                    if c.est_m3 < c.capacity:
                        c.notified = False  # re-arm alert after compressing
                    c.last_event = now
                    dirty = True
                elif isinstance(ev, UnknownOreEvent):
                    c = self.char(ev.character)
                    c.unknown_ores[ev.ore] = c.unknown_ores.get(ev.ore, 0) + ev.qty
                elif isinstance(ev, ResidueEvent):
                    self.stats["residue_events"] = (
                        self.stats.get("residue_events", 0) + 1)
                    # NOTE: no est_m3 change - residue never enters the hold.
        if dirty:
            self.save_state()
        if ledger_dirty:
            self.save_ledger()
        return events

    def _parse_line(self, character: str, line: str):
        m = LINE_RE.match(line)
        if not m:
            return None
        channel = m.group("channel").strip().lower()
        if channel == "combat":
            return self._parse_combat(character, m) if self.combat_enabled else None
        # Jump lines arrive on the "(None)" channel, so this must come before
        # the MINING_CHANNELS filter below.
        jm = SYSTEM_CHANGE_RE.search(TAG_RE.sub("", m.group("msg")).strip())
        if jm:
            return SystemChangeEvent(character=character,
                                     to_system=jm.group("to_sys").strip(),
                                     ts=m.group("ts"))
        if channel not in MINING_CHANNELS:
            return None
        msg = TAG_RE.sub("", m.group("msg")).strip()
        low = msg.lower()
        if channel == "notify" and any(k in low for k in HOLD_FULL_MARKERS):
            return HoldFullEvent(character=character, ts=m.group("ts"))
        if (channel == "notify" and self.drone_enabled and
                DRONE_STOP_RE.search(msg)):
            return DroneStopEvent(character=character, ts=m.group("ts"))
        cm = COMPRESS_RE.search(msg)
        if cm:
            qty = parse_qty(cm.group("qty"))
            ore = cm.group("ore").strip()
            raw_vol = self.table.unit_volume(ore)
            if qty <= 0 or raw_vol is None:
                return None
            if self.compressed_leaves_hold:
                delta = -qty * raw_vol
            else:
                comp_vol = self.table.unit_volume("Compressed " + ore)
                if comp_vol is None:
                    comp_vol = raw_vol / 100.0
                delta = qty * (comp_vol - raw_vol)
            return CompressionEvent(character=character, qty=qty, ore=ore,
                                    delta_m3=delta, ts=m.group("ts"))
        # Must come BEFORE the EXCLUDE_MARKERS check: "residue" is in that
        # list (correctly - these units never enter the hold), but they do
        # deplete the asteroid, so a scanned rock has to see them.
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
        if any(k in low for k in EXCLUDE_MARKERS):
            return None
        for pat in self.patterns:
            pm = pat.search(msg)
            if not pm:
                continue
            qty = parse_qty(pm.group("qty"))
            ore = pm.group("ore").strip()
            if qty <= 0 or not ore:
                return None
            vol = self.table.unit_volume(ore)
            if vol is None:
                log.warning("unknown ore '%s' (qty %d) from %s", ore, qty, character)
                return UnknownOreEvent(character=character, ore=ore, qty=qty)
            self._last_tick[character] = (m.group("ts"), ore)
            return MiningEvent(character=character, qty=qty, ore=ore,
                               m3=qty * vol, ts=m.group("ts"))
        if channel == "mining":
            # a (mining) line none of our patterns matched - the one thing
            # we most need to see when diagnosing "nothing is changing"
            self.stats["unmatched_mining"] += 1
            if self._unmatched_logged < 25:
                self._unmatched_logged += 1
                log.warning("UNMATCHED mining line from %s: %r", character, msg)
        return None

    def _parse_combat(self, character: str, m):
        """Incoming aggression only. Outgoing lines say 'N to X' and never
        match. is_player uses the Name[CORP](Ship) rendering; plain-named
        NPC rats stay is_player=False and are never alerted on."""
        msg = TAG_RE.sub("", m.group("msg")).strip()
        dm = COMBAT_DMG_RE.match(msg)
        who, kind = None, None
        if dm:
            who, kind = dm.group("who").strip(), "damage"
        else:
            em = COMBAT_EWAR_RE.match(msg)
            if em:
                who, kind = em.group("who").strip(), em.group("kind").lower()
        if not who:
            return None
        is_player = bool(PLAYER_ATTACKER_RE.search(who))
        if is_player:
            log.info("combat: %s <- %s (%s)", character, who, kind)
        return CombatEvent(character=character, attacker=who, kind=kind,
                           is_player=is_player, ts=m.group("ts"))
