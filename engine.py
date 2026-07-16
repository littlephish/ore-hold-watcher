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


HOLD_FULL_MARKERS = (
    "ore hold is full",
    "mining hold is full",
    "cargo hold is full",
    "is full and cannot accept",
)

# lines that look like mining but must NOT be counted
EXCLUDE_MARKERS = ("residue", "wasted", "lost")


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
class CompressionEvent:
    character: str
    qty: int          # compressed units produced == raw units consumed (1:1)
    ore: str          # raw ore name
    delta_m3: float   # negative: how much the hold shrank
    ts: str


# ---------------------------------------------------------------------------
# Ore volume lookup
# ---------------------------------------------------------------------------

class OreTable:
    def __init__(self, override_path: Path | None = None):
        self.base = {k.lower(): v for k, v in ores.ORE_VOLUMES.items()}
        self.compressed = {k.lower(): v for k, v in ores.COMPRESSED_VOLUMES.items()}
        self.overrides: dict[str, float] = {}
        if override_path and override_path.exists():
            try:
                data = json.loads(override_path.read_text(encoding="utf-8"))
                self.overrides = {str(k).lower(): float(v) for k, v in data.items()}
            except Exception:
                pass  # a broken override file should never kill the app

    def unit_volume(self, name: str) -> float | None:
        n = " ".join(name.split()).strip().strip(".*").lower()
        if not n:
            return None
        if n in self.overrides:
            return self.overrides[n]
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
        return self._suffix_lookup(n, self.base)

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


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class Engine:
    def __init__(self, log_dir: Path, state_path: Path,
                 ore_override_path: Path | None = None,
                 mining_patterns: list[str] | None = None,
                 lookback_hours: float = 24.0,
                 default_capacity: float = 180000.0,
                 compressed_leaves_hold: bool = True):
        self.log_dir = Path(log_dir)
        self.state_path = Path(state_path)
        self.lookback_hours = lookback_hours
        self.default_capacity = default_capacity
        # True = user moves compressed ore out of the ore hold (fleet hangar,
        # etc.) right after compressing, so a compression frees the FULL raw
        # volume. False = compressed stacks stay in the ore hold at their
        # (tiny) compressed volume.
        self.compressed_leaves_hold = compressed_leaves_hold
        self.table = OreTable(ore_override_path)
        pats = mining_patterns or DEFAULT_MINING_PATTERNS
        self.patterns = [re.compile(p, re.IGNORECASE) for p in pats]
        self.files: dict[str, LogFile] = {}
        self.chars: dict[str, CharacterState] = {}
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

    def save_state(self):
        data = {"characters": {
            c.name: {"capacity": c.capacity, "last_event": c.last_event,
                     "notified": c.notified, "anchor_ts": c.anchor_ts,
                     "anchor_m3": c.anchor_m3}
            for c in self.chars.values()}}
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

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
        for lf in list(self.files.values()):
            lines = lf.read_new_lines()
            if not lines:
                continue
            self.stats["lines"] += len(lines)
            name = lf.character or lf.path.stem
            for ln in lines:
                ev = self._parse_line(name, ln)
                if ev is None:
                    continue
                # anchor filter: log events at/before a character's last
                # reset/calibration are already baked into anchor_m3 -
                # skipping them makes startup replay idempotent
                ev_ts = getattr(ev, "ts", None)
                if ev_ts is not None:
                    c = self.char(ev.character)
                    if c.anchor_ts and ev_ts <= c.anchor_ts:
                        continue
                events.append(ev)
                self.stats["last_event_wall"] = now
                if isinstance(ev, MiningEvent):
                    self.stats["mining_events"] += 1
                    log.debug("mining: %s +%d %s = %.1f m3",
                              ev.character, ev.qty, ev.ore, ev.m3)
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
        if dirty:
            self.save_state()
        return events

    def _parse_line(self, character: str, line: str):
        m = LINE_RE.match(line)
        if not m:
            return None
        channel = m.group("channel").strip().lower()
        if channel not in MINING_CHANNELS:
            return None
        msg = TAG_RE.sub("", m.group("msg")).strip()
        low = msg.lower()
        if channel == "notify" and any(k in low for k in HOLD_FULL_MARKERS):
            return HoldFullEvent(character=character, ts=m.group("ts"))
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
