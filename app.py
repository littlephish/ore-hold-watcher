"""Ore Hold Watcher - local EVE Online ore hold tracker.

Sits in the system tray, tails your EVE gamelogs, estimates each character's
ore hold fill, and pops a Windows notification when a hold crosses the alert
threshold. No Discord, no ESI, fully local.

Run:      pythonw app.py         (or use run.bat)
Package:  build.bat              (Nuitka onefile exe)
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("orewatcher.app")

from PySide6.QtCore import Qt, QTimer, QSize, QRect
from PySide6.QtGui import (QAction, QBrush, QColor, QIcon, QPainter, QPen,
                           QPixmap, QFont)
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QMainWindow, QMenu, QMessageBox, QProgressBar,
                               QPushButton, QScrollArea, QSpinBox,
                               QSystemTrayIcon, QVBoxLayout, QWidget,
                               QCheckBox)

from engine import (Engine, MiningEvent, HoldFullEvent, UnknownOreEvent,
                    CombatEvent, DroneStopEvent, ts_to_epoch)

APP_NAME = "Ore Hold Watcher"
ORG_DIR = "OreHoldWatcher"

try:
    from winotify import Notification, audio  # Windows toasts
    HAVE_WINOTIFY = sys.platform == "win32"
except Exception:
    HAVE_WINOTIFY = False


def _appdata_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / ORG_DIR


def app_base_dir() -> Path:
    """Folder the exe lives in (Nuitka/frozen) or the source folder."""
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def _writable(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".write_test"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


_CONFIG_DIR: Path | None = None
_CONFIG_FILES = ("settings.json", "state.json", "ores_override.json")


def config_dir() -> Path:
    """Config lives BESIDE THE EXE (portable). Falls back to APPDATA when
    that folder isn't writable. Existing APPDATA config is migrated (copied)
    the first time, so nothing is lost."""
    global _CONFIG_DIR
    if _CONFIG_DIR is not None:
        return _CONFIG_DIR
    portable = app_base_dir()
    if _writable(portable):
        d = portable
        old = _appdata_dir()
        if old.is_dir() and not (d / "settings.json").exists():
            import shutil
            for f in _CONFIG_FILES:
                src = old / f
                if src.exists() and not (d / f).exists():
                    try:
                        shutil.copy2(src, d / f)
                    except OSError:
                        pass
    else:
        d = _appdata_dir()
        d.mkdir(parents=True, exist_ok=True)
    _CONFIG_DIR = d
    return d


_LOG_FMT = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
_FILE_HANDLER: logging.Handler | None = None


def set_file_logging(enabled: bool):
    """Attach/detach the debug.log file handler. When debug logging is
    disabled, nothing is written to disk at all."""
    global _FILE_HANDLER
    root = logging.getLogger("orewatcher")
    if enabled and _FILE_HANDLER is None:
        fh = logging.handlers.RotatingFileHandler(
            config_dir() / "debug.log", maxBytes=1_000_000, backupCount=3,
            encoding="utf-8")
        fh.setFormatter(_LOG_FMT)
        root.addHandler(fh)
        _FILE_HANDLER = fh
    elif not enabled and _FILE_HANDLER is not None:
        root.removeHandler(_FILE_HANDLER)
        _FILE_HANDLER.close()
        _FILE_HANDLER = None


def setup_logging(verbose: bool):
    """stderr always; debug.log only while debug logging is enabled."""
    root = logging.getLogger("orewatcher")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh = logging.StreamHandler()
    sh.setFormatter(_LOG_FMT)
    root.addHandler(sh)
    set_file_logging(verbose)


def _documents_candidates() -> list[Path]:
    """Possible Documents folders, best first. No vendor paths hardcoded:
    the Windows known-folder API already follows OneDrive/redirected
    Documents; the %OneDrive% env var covers odd setups; plain
    ~/Documents is the final fallback."""
    cands: list[Path] = []
    if sys.platform == "win32":
        try:  # authoritative: SHGetKnownFolderPath(FOLDERID_Documents)
            import ctypes
            from ctypes import wintypes
            # FOLDERID_Documents {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
            class GUID(ctypes.Structure):
                _fields_ = [("D1", ctypes.c_uint32), ("D2", ctypes.c_uint16),
                            ("D3", ctypes.c_uint16), ("D4", ctypes.c_ubyte * 8)]
            g = GUID(0xFDD39AD0, 0x238F, 0x46AF,
                     (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
            out = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(
                    ctypes.byref(g), 0, None, ctypes.byref(out)) == 0:
                cands.append(Path(out.value))
                ctypes.windll.ole32.CoTaskMemFree(out)
        except Exception as e:
            log.debug("known-folder lookup failed: %s", e)
        onedrive = os.environ.get("OneDrive")
        if onedrive:
            cands.append(Path(onedrive) / "Documents")
    cands.append(Path.home() / "Documents")
    return cands


def detect_log_dir() -> Path:
    """First candidate whose EVE/logs/Gamelogs exists; else the default."""
    seen = set()
    for docs in _documents_candidates():
        d = docs / "EVE" / "logs" / "Gamelogs"
        if str(d) in seen:
            continue
        seen.add(str(d))
        if d.is_dir():
            log.info("auto-detected gamelogs folder: %s", d)
            return d
        log.info("no gamelogs at candidate: %s", d)
    return Path.home() / "Documents" / "EVE" / "logs" / "Gamelogs"


DEFAULT_SETTINGS = {
    "log_dir": "",   # "" = auto-detect the active user's Documents/EVE/logs/Gamelogs
    "debug_verbose": False,
    "threshold_pct": 90.0,
    "rearm_margin_pct": 5.0,
    "default_capacity": 180000.0,
    "poll_seconds": 2,
    "lookback_hours": 24,
    "always_on_top": False,
    "compressed_leaves_hold": True,  # you drag compressed ore to fleet hangar
    "alert_interval_min": 5.0,  # at most one alert per X minutes (0 = every alert)
    "idle_alert_enabled": True,  # alert when a pilot stops receiving ore ticks
    "idle_alert_min": 5.0,       # ... for this many minutes
    "combat_alert_enabled": False,  # scan/alert on PLAYER aggression (never NPC)
    "combat_alert_cooldown_s": 120,  # per-pilot cooldown between combat alerts
    "drone_alert_enabled": False,   # alert when mining drones stop (rock depleted)
    "drone_alert_cooldown_s": 30,   # debounce a whole flight stopping at once
    "ledger_enabled": True,        # daily per-character mined-ore ledger
    "ledger_fetch_prices": True,   # Jita prices via Fuzzwork for ISK (on)
    "ledger_backfill_prices": True,  # value unpriced past days at today's price
    "client_watch_enabled": True,  # read window titles ("EVE - Name") to know
                                   # which characters are actually logged in
    # --- auto-close before EVE daily downtime (cluster shutdown 11:00 UTC) ---
    "close_before_downtime": False,       # OFF by default
    "close_minutes_before": 5.0,          # force-close X min before shutdown
    "downtime_utc": "11:00",              # daily cluster shutdown, UTC
    "eve_process_names": ["exefile.exe"],  # EVE client process name(s)
    # --- alert methods (each independently toggleable) ---
    "notify_popup": True,      # Windows toast (or tray balloon fallback)
    "notify_overlay": False,   # always-on-top banner in the screen corner
    "notify_sound": True,      # built-in system ding
    "notify_webhook": False,   # HTTP POST (Discord webhook URLs auto-detected)
    "webhook_url": "",
    "discord_mention": "everyone",  # "everyone" | "custom" | "none"
    "discord_mention_id": "",       # user ID, @here, or <@&roleID> when custom
    "notify_ntfy": False,      # push to your phone via ntfy.sh
    "ntfy_topic": "",
    "hide_idle_hours": 12,   # hide chars with no activity for this long (0 = never hide)
    "update_check": True,    # check GitHub releases for a newer exe
    "update_repo": "",       # "owner/repo" of this app on GitHub
    "window_size": [560, 500],  # remembered across runs
    "mining_patterns": [],    # optional custom regexes; empty = built-in defaults
}


class Settings:
    def __init__(self):
        self.path = config_dir() / "settings.json"
        self.data = dict(DEFAULT_SETTINGS)
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.data.update(raw)
                # migrate pre-methods "notifications" master switch
                if "notify_popup" not in raw and "notifications" in raw:
                    self.data["notify_popup"] = bool(raw["notifications"])
            except Exception:
                pass
        else:
            self.save()

    def save(self):
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    def __getitem__(self, k):
        return self.data.get(k, DEFAULT_SETTINGS.get(k))

    def __setitem__(self, k, v):
        self.data[k] = v


# ---------------------------------------------------------------------------
# Styling
# ---------------------------------------------------------------------------

DARK_QSS = """
QMainWindow, QDialog { background: #2b2d31; }
QWidget { color: #dbdee1; font-size: 13px; }
QLabel#charName { font-weight: 600; font-size: 13px; }
QLabel#amount { color: #949ba4; font-size: 12px; }
QLabel#pctChip {
    background: #1e1f22; color: #dbdee1; border-radius: 4px;
    padding: 1px 6px; font-weight: 700; font-size: 12px;
}
QFrame#row { background: #313338; border-radius: 8px; }
QProgressBar {
    background: #1e1f22; border: none; border-radius: 4px;
    height: 8px; text-align: center;
}
QProgressBar::chunk { border-radius: 4px; }
QPushButton {
    background: #4e5058; border: none; border-radius: 4px;
    padding: 5px 12px; color: #fff;
}
QLineEdit, QDoubleSpinBox, QSpinBox, QComboBox {
    background: #ffffff; color: #000000;
    border: 1px solid #1e1f22; border-radius: 4px; padding: 3px 6px;
    selection-background-color: #5865f2; selection-color: #ffffff;
}
QLineEdit::placeholder { color: #6d6f78; }
QComboBox QAbstractItemView {
    background: #1e1f22; color: #dbdee1;
    border: 1px solid #404249; border-radius: 4px; outline: none;
    selection-background-color: #5865f2; selection-color: #ffffff;
}
QComboBox QAbstractItemView::item {
    color: #dbdee1; background: #1e1f22; padding: 5px 8px;
}
QComboBox QAbstractItemView::item:hover,
QComboBox QAbstractItemView::item:selected {
    color: #ffffff; background: #5865f2;
}
QComboBox::drop-down { border: none; width: 22px; }
QCheckBox { spacing: 8px; }
QTabWidget::pane { border: 1px solid #1e1f22; border-radius: 4px; }
QTabBar::tab {
    background: #2b2d31; color: #949ba4; padding: 6px 14px;
    border-top-left-radius: 4px; border-top-right-radius: 4px;
}
QTabBar::tab:selected { background: #404249; color: #ffffff; }
QTreeWidget {
    background: #1e1f22; alternate-background-color: #232428;
    border: 1px solid #1e1f22; border-radius: 4px;
}
QTreeWidget::item { padding: 3px 6px; }
QTreeWidget::item:selected { background: #5865f2; color: #ffffff; }
QHeaderView::section {
    background: #2b2d31; color: #949ba4; border: none;
    padding: 4px 6px; font-weight: 700;
}
QPushButton:hover { background: #6d6f78; }
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: #2b2d31; }
QScrollBar:vertical, QScrollBar:horizontal {
    background: #2b2d31; border: none; width: 10px; height: 10px;
}
QScrollBar::handle {
    background: #4e5058; border-radius: 5px; min-height: 24px; min-width: 24px;
}
QScrollBar::handle:hover { background: #6d6f78; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QMenu { background: #2b2d31; border: 1px solid #1e1f22; }
QMenu::item:selected { background: #404249; }
"""


def style_titlebar(win):
    """Match the native Windows title bar to the app's dark theme.
    Win11: exact caption/text colors; Win10 (1809+): dark mode fallback."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = int(win.winId())
        dwm = ctypes.windll.dwmapi
        dark = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (20; 19 pre-20H1)
            if dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(dark), 4) == 0:
                break
        # Win11 22000+: precise colors (harmlessly rejected on Win10)
        caption = ctypes.c_uint(0x00312D2B)  # #2b2d31 as COLORREF (BGR)
        text = ctypes.c_uint(0x00E1DEDB)     # #dbdee1
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption), 4)
        dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(text), 4)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Logged-in client detection (window titles, read-only)
# ---------------------------------------------------------------------------

class ClientWatcher:
    """Enumerates top-level windows belonging to EVE client processes and
    reads their titles ("EVE - CharacterName" when logged in, "EVE" at
    character select). Pure read-only Win32 window-manager calls; the EVE
    process itself is never touched."""

    def __init__(self, process_names: list[str]):
        self.process_names = {str(n).lower() for n in (process_names or
                                                       ["exefile.exe"])}
        self.online: set[str] = set()   # character names with a live window
        self.clients = 0                # EVE windows seen (incl. char select)
        self.ready = False              # at least one successful refresh

    def refresh(self):
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes
            user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
            found: set[str] = set()
            count = [0]

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def enum_cb(hwnd, _):
                if not user32.IsWindowVisible(hwnd):
                    return True
                n = user32.GetWindowTextLengthW(hwnd)
                if not n:
                    return True
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                title = buf.value
                if not title.lower().startswith("eve"):
                    return True
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                h = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED
                if not h:
                    return True
                try:
                    size = wintypes.DWORD(1024)
                    pbuf = ctypes.create_unicode_buffer(size.value)
                    if kernel32.QueryFullProcessImageNameW(
                            h, 0, pbuf, ctypes.byref(size)):
                        exe = pbuf.value.rsplit("\\", 1)[-1].lower()
                        if exe in self.process_names:
                            count[0] += 1
                            if " - " in title:
                                found.add(title.split(" - ", 1)[1].strip())
                finally:
                    kernel32.CloseHandle(h)
                return True

            user32.EnumWindows(enum_cb, 0)
            self.online = found
            self.clients = count[0]
            self.ready = True
        except Exception as e:
            log.warning("client watch failed: %s", e)


# ---------------------------------------------------------------------------
# Jita prices (Fuzzwork, opt-in)
# ---------------------------------------------------------------------------

class PriceService:
    """Resolves ore names to Jita buy.max ISK via Fuzzwork's public APIs.
    Everything is cached to prices.json; network only runs when the user
    has opted in, and only when the cache is older than 12 hours."""

    REGION = 10000002  # The Forge (Jita)

    def __init__(self):
        self.path = config_dir() / "prices.json"
        # "ts" = last time ANY refresh attempt got fresh data (drives the
        # 12 h refresh cadence). "ok_ts" = last FULLY successful refresh
        # (drives the staleness label). "prices" is never cleared on a
        # failure - a known price is kept until a newer one replaces it.
        self.data = {"ts": 0.0, "ok_ts": 0.0, "ids": {}, "prices": {}}
        self.busy = False
        self.error: str | None = None
        try:
            if self.path.exists():
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
        except Exception:
            pass

    def cached(self) -> dict:
        return dict(self.data.get("prices", {}))

    def stale(self) -> bool:
        return time.time() - float(self.data.get("ts", 0)) > 12 * 3600

    def age_seconds(self) -> float | None:
        """How old the last successfully fetched price is, or None if we've
        never fetched one."""
        ok = float(self.data.get("ok_ts", 0))
        return (time.time() - ok) if ok else None

    def _save(self):
        try:
            self.path.write_text(json.dumps(self.data, indent=1),
                                 encoding="utf-8")
        except OSError as e:
            log.warning("prices.json save failed (keeping in memory): %s", e)

    def fetch_async(self, names: list[str]):
        if self.busy:
            return
        self.busy = True
        self.error = None
        threading.Thread(target=self._fetch, args=(list(names),),
                         daemon=True).start()

    def _fetch(self, names: list[str]):
        """Best-effort refresh. Every failure mode keeps the last known
        prices: a bad type-ID lookup skips that one ore, a dead market API
        leaves ALL prices untouched, and a disk error keeps them in memory.
        We never zero or delete a price we already have."""
        import urllib.parse
        updated = 0
        try:
            ids = self.data.setdefault("ids", {})
            # resolve missing type IDs, one ore at a time so a single bad
            # name or blip can't abort the batch
            for name in names:
                if name in ids:
                    continue
                try:
                    url = ("https://www.fuzzwork.co.uk/api/typeid.php?typename="
                           + urllib.parse.quote(name))
                    req = urllib.request.Request(
                        url, headers={"User-Agent": APP_NAME})
                    with urllib.request.urlopen(req, timeout=15) as r:
                        d = json.loads(r.read().decode("utf-8"))
                    tid = int(d.get("typeID", 0) or 0)
                    if tid:
                        ids[name] = tid
                    else:
                        log.warning("no typeID for ore %r", name)
                except Exception as e:
                    log.warning("typeID lookup for %r failed (keeping any "
                                "known price): %s", name, e)

            wanted = {n: ids[n] for n in names if n in ids}
            got_market = False
            if wanted:
                try:
                    url = (f"https://market.fuzzwork.co.uk/aggregates/?region="
                           f"{self.REGION}&types="
                           + ",".join(str(t) for t in wanted.values()))
                    req = urllib.request.Request(
                        url, headers={"User-Agent": APP_NAME})
                    with urllib.request.urlopen(req, timeout=20) as r:
                        agg = json.loads(r.read().decode("utf-8"))
                    got_market = True
                    for name, tid in wanted.items():
                        entry = agg.get(str(tid), {})
                        buy = float(entry.get("buy", {}).get("max", 0) or 0)
                        if buy > 0:          # only overwrite with a real price
                            self.data["prices"][name] = buy
                            updated += 1
                except Exception as e:
                    log.warning("market fetch failed - keeping %d cached "
                                "prices: %s", len(self.data["prices"]), e)
                    self.error = str(e)

            if got_market:
                now = time.time()
                self.data["ts"] = now
                self.data["ok_ts"] = now
                self.error = None
                self._save()
                log.info("prices refreshed: %d updated, %d total cached",
                         updated, len(self.data["prices"]))
            elif updated == 0 and not self.data["prices"]:
                self.error = self.error or "no price data available yet"
        except Exception as e:   # never let the price thread crash the app
            log.warning("price fetch aborted (cache preserved): %s", e)
            self.error = str(e)
        finally:
            self.busy = False


# ---------------------------------------------------------------------------
# Auto-update (GitHub releases)
# ---------------------------------------------------------------------------

def parse_ver(s: str) -> tuple:
    nums = re.findall(r"\d+", s or "")
    return tuple(int(n) for n in nums[:4]) if nums else (0,)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


def current_exe_version() -> str | None:
    """Version stamped into the running exe by the release build.
    None when running from source (auto-update is disabled then)."""
    if not (is_frozen() and sys.platform == "win32"):
        return None
    try:
        import ctypes
        path = sys.executable
        size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        ctypes.windll.version.GetFileVersionInfoW(path, 0, size, buf)
        val = ctypes.c_void_p()
        vlen = ctypes.c_uint()
        if not ctypes.windll.version.VerQueryValueW(
                buf, "\\", ctypes.byref(val), ctypes.byref(vlen)):
            return None

        class VSFixed(ctypes.Structure):
            _fields_ = [("sig", ctypes.c_uint32), ("strucver", ctypes.c_uint32),
                        ("ms", ctypes.c_uint32), ("ls", ctypes.c_uint32),
                        ("pms", ctypes.c_uint32), ("pls", ctypes.c_uint32),
                        ("rest", ctypes.c_uint32 * 7)]
        ffi = ctypes.cast(val, ctypes.POINTER(VSFixed)).contents
        return f"{ffi.ms >> 16}.{ffi.ms & 0xFFFF}.{ffi.ls >> 16}"
    except Exception as e:
        log.debug("exe version lookup failed: %s", e)
        return None


class Updater:
    """Checks GitHub releases, downloads the new exe, swaps it in place.
    Network work runs in daemon threads; the UI polls the fields."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.busy = False
        self.available: dict | None = None   # {"version", "url"}
        self.up_to_date: str | None = None   # latest tag when already current
        self.error: str | None = None
        self.downloaded: str | None = None   # path of the fetched .new file
        self.manual = False

    def repo(self) -> str:
        return str(self.s["update_repo"]).strip().strip("/")

    def can_update(self) -> bool:
        return bool(self.repo()) and is_frozen() and sys.platform == "win32"

    # -- phase 1: check ------------------------------------------------------
    def check_async(self, manual: bool = False):
        if self.busy or not self.repo():
            return
        self.busy = True
        self.manual = manual
        threading.Thread(target=self._check, daemon=True).start()

    def _check(self):
        try:
            url = f"https://api.github.com/repos/{self.repo()}/releases/latest"
            req = urllib.request.Request(url, headers={
                "User-Agent": APP_NAME,
                "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
            tag = str(data.get("tag_name", ""))
            asset = next((a for a in data.get("assets", [])
                          if a.get("name", "").lower().endswith(".exe")), None)
            cur = current_exe_version() or "0"
            log.info("update check: current=%s latest=%s", cur, tag)
            if asset and parse_ver(tag) > parse_ver(cur):
                self.available = {"version": tag,
                                  "url": asset["browser_download_url"],
                                  "current": cur}
            else:
                self.up_to_date = tag or "unknown"
        except Exception as e:
            log.warning("update check failed: %s", e)
            self.error = str(e)
        finally:
            self.busy = False

    # -- phase 2: download ---------------------------------------------------
    def download_async(self, url: str):
        if self.busy:
            return
        self.busy = True
        threading.Thread(target=self._download, args=(url,),
                         daemon=True).start()

    def _download(self, url: str):
        try:
            dest = sys.executable + ".new"
            req = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
            with urllib.request.urlopen(req, timeout=300) as r, \
                    open(dest, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            if os.path.getsize(dest) < 5_000_000:  # sanity: a real exe is big
                raise ValueError("downloaded file suspiciously small")
            self.downloaded = dest
            log.info("update downloaded: %s (%d bytes)", dest,
                     os.path.getsize(dest))
        except Exception as e:
            log.warning("update download failed: %s", e)
            self.error = str(e)
        finally:
            self.busy = False

    # -- phase 3: swap + restart ---------------------------------------------
    def apply(self) -> bool:
        """Write a swap script that waits for this process to exit, replaces
        the exe, and relaunches it. Caller must quit right after."""
        if not self.downloaded:
            return False
        import subprocess
        exe = sys.executable
        bat = exe + ".update.bat"
        pid = os.getpid()
        with open(bat, "w", encoding="ascii") as f:
            f.write(f"""@echo off
:wait
tasklist /fi "PID eq {pid}" 2>nul | find "{pid}" >nul
if not errorlevel 1 (
    timeout /t 1 /nobreak >nul
    goto wait
)
move /y "{self.downloaded}" "{exe}" >nul
start "" "{exe}"
del "%~f0"
""")
        subprocess.Popen(
            ["cmd", "/c", bat],
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                           | getattr(subprocess, "DETACHED_PROCESS", 0)),
            close_fds=True)
        log.info("update swap script launched; exiting for replacement")
        return True


class DarkDialog(QDialog):
    """QDialog with the themed native title bar."""

    def showEvent(self, ev):
        super().showEvent(ev)
        style_titlebar(self)


def fmt_eta(seconds: float) -> str:
    s = int(seconds)
    if s <= 0:
        return "FULL"
    h, rem = divmod(s, 3600)
    m = rem // 60
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m"
    return "<1m"


def fill_color(pct: float) -> str:
    if pct >= 90:
        return "#f23f43"   # red
    if pct >= 75:
        return "#f0b232"   # amber
    return "#23a55a"       # green


def make_tray_icon(pct: float) -> QIcon:
    """Donut gauge colored by the fullest character."""
    size = 64
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    rect = pm.rect().adjusted(6, 6, -6, -6)
    p.setPen(QPen(QColor("#3f4147"), 10))
    p.drawArc(rect, 0, 360 * 16)
    if pct > 0:
        p.setPen(QPen(QColor(fill_color(pct)), 10, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(rect, 90 * 16, -int(360 * 16 * min(pct, 100) / 100))
    p.setPen(QColor("#dbdee1"))
    f = QFont()
    f.setPixelSize(22)
    f.setBold(True)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, f"{int(round(min(pct, 99)))}")
    p.end()
    return QIcon(pm)


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------

class OverlayBanner(QWidget):
    """Frameless always-on-top banner in the top-right screen corner."""

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.Tool)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.label = QLabel("", self)
        self.label.setWordWrap(True)
        self.label.setStyleSheet(
            "background: #f23f43; color: white; font-size: 16px; "
            "font-weight: 700; border-radius: 10px; padding: 16px 22px;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.label)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def mousePressEvent(self, ev):  # click to dismiss
        self.hide()

    def show_alert(self, text: str, msec: int = 10000):
        self.label.setText(text)
        self.adjustSize()
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 24, screen.top() + 24)
        self.show()
        self.raise_()
        self._timer.start(msec)


def _post_json(url: str, payload: dict, timeout: float = 10.0):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json",
                                 "User-Agent": APP_NAME})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


class Notifier:
    """Fans one alert out to every enabled method. Network sends run in
    daemon threads so the UI never blocks; failures only hit debug.log."""

    def __init__(self, settings: Settings, tray: QSystemTrayIcon):
        self.s = settings
        self.tray = tray
        self.overlay = OverlayBanner()

    def alert(self, title: str, body: str, payload: dict | None = None):
        log.info("ALERT: %s | %s", title, body)
        if self.s["notify_popup"]:
            self._popup(title, body)
        if self.s["notify_overlay"]:
            self.overlay.show_alert(f"{title}\n{body}")
        if self.s["notify_sound"]:
            self._ding()
        if self.s["notify_webhook"] and str(self.s["webhook_url"]).strip():
            threading.Thread(target=self._webhook,
                             args=(title, body, payload or {}),
                             daemon=True).start()
        if self.s["notify_ntfy"] and str(self.s["ntfy_topic"]).strip():
            threading.Thread(target=self._ntfy, args=(title, body),
                             daemon=True).start()

    # -- methods -------------------------------------------------------------
    def _popup(self, title: str, body: str):
        if HAVE_WINOTIFY:
            try:
                t = Notification(app_id=APP_NAME, title=title, msg=body)
                t.show()
                return
            except Exception as e:
                log.warning("toast failed: %s", e)
        self.tray.showMessage(title, body, QSystemTrayIcon.Warning, 8000)

    def _ding(self):
        try:
            if sys.platform == "win32":
                import winsound
                winsound.PlaySound("SystemExclamation",
                                   winsound.SND_ALIAS | winsound.SND_ASYNC)
                return
        except Exception as e:
            log.warning("sound failed: %s", e)
        QApplication.beep()

    def _webhook(self, title: str, body: str, payload: dict):
        url = str(self.s["webhook_url"]).strip()
        try:
            if "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url:
                data = self._discord_body(title, body, payload,
                                          mention=self._mention_string())
            elif "maker.ifttt.com/trigger/" in url and "/json/" not in url:
                # IFTTT classic trigger: only value1/value2/value3 map to
                # applet ingredients. (An IFTTT URL WITH /json/ takes the
                # generic payload below and parses it with filter code.)
                fullest = ""
                chars = (payload or {}).get("characters")
                if chars:
                    c = chars[0]
                    fullest = f"{c['character']} {c['pct']}% ({c['est_m3']:,} m³)"
                data = {"value1": title, "value2": body, "value3": fullest}
            else:
                data = {"title": title, "message": body, **payload}
            status = _post_json(url, data)
            log.info("webhook sent (%s)", status)
        except Exception as e:
            log.warning("webhook failed: %s", e)

    def _mention_string(self) -> str:
        """Build the Discord mention from settings. '' = no ping."""
        mode = str(self.s["discord_mention"]).lower()
        if mode == "none":
            return ""
        if mode == "custom":
            raw = str(self.s["discord_mention_id"]).strip()
            if not raw:
                return ""
            if raw.isdigit():
                return f"<@{raw}>"          # numeric user ID -> real ping
            return raw                       # @here, <@&roleID>, etc. as-is
        return "@everyone"

    @staticmethod
    def _discord_body(title: str, body: str, payload: dict,
                      mention: str = "@everyone") -> dict:
        """Discord embed: one line per character with a status dot,
        embed color = worst character's state."""
        chars = (payload or {}).get("characters")
        if chars:
            def dot(p):
                return "🔴" if p >= 90 else ("🟡" if p >= 75 else "🟢")
            def eta_txt(c):
                m = c.get("eta_min")
                if m is None:
                    return ""
                if m <= 0:
                    return " · **FULL**"
                return f" · full in {m//60}h {m%60:02d}m" if m >= 60 else f" · full in {m}m"
            lines = [f"{dot(c['pct'])} `{c['pct']:5.1f}%` **{c['character']}** - "
                     f"~{c['est_m3']:,} / {c['capacity_m3']:,} m³{eta_txt(c)}"
                     for c in chars]
            desc = "\n".join(lines)
            max_pct = max(c["pct"] for c in chars)
            color = 0xF23F43 if max_pct >= 90 else (
                0xF0B232 if max_pct >= 75 else 0x23A55A)
        else:
            desc = body
            color = 0xF0B232
        data = {"embeds": [{"title": title, "description": desc[:4000],
                            "color": color,
                            "footer": {"text": "Ore Hold Watcher"}}]}
        if mention:
            data["content"] = mention
        return data

    def _ntfy(self, title: str, body: str):
        topic = str(self.s["ntfy_topic"]).strip().lstrip("/")
        url = topic if topic.startswith("http") else f"https://ntfy.sh/{topic}"
        try:
            req = urllib.request.Request(
                url, data=body.encode("utf-8"),
                headers={"Title": title, "Priority": "high",
                         "Tags": "warning", "User-Agent": APP_NAME})
            with urllib.request.urlopen(req, timeout=10) as r:
                log.info("ntfy sent (%s)", r.status)
        except Exception as e:
            log.warning("ntfy failed: %s", e)


# ---------------------------------------------------------------------------
# Character row widget
# ---------------------------------------------------------------------------

class CharRow(QWidget):
    def __init__(self, main: "MainWindow", name: str):
        super().__init__()
        self.main = main
        self.name = name
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.menu)

        self.dot = QLabel("●")
        self.chip = QLabel("0.0%")
        self.chip.setObjectName("pctChip")
        self.lbl = QLabel(name)
        self.lbl.setObjectName("charName")
        self.arm = QLabel("")
        self.arm.setObjectName("pctChip")
        self.arm.setToolTip("Idle-alert status: armed = watching for a stop "
                            "in ore ticks; idle = ticks stopped (alert "
                            "sent); standby = no live ticks yet")
        self.amount = QLabel("")
        self.amount.setObjectName("amount")
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.dot)
        top.addWidget(self.chip)
        top.addWidget(self.lbl)
        top.addWidget(self.arm)
        top.addStretch(1)
        top.addWidget(self.amount)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(4)
        lay.addLayout(top)
        lay.addWidget(self.bar)

    ARM_STYLES = {
        "armed":   ("⛏ armed",   "#23a55a"),
        "idle":    ("⏸ idle",    "#f0b232"),
        "closed":  ("🔌 closed", "#949ba4"),
        "standby": ("standby",   "#6d6f78"),
    }

    def update_state(self, est: float, cap: float, eta_s: float | None = None,
                     arm_state: str | None = None):
        pct = 100.0 * est / cap if cap else 0.0
        col = fill_color(pct)
        self.dot.setStyleSheet(f"color: {col}; font-size: 14px;")
        self.chip.setText(f"{pct:.1f}%")
        if arm_state in self.ARM_STYLES:
            txt, acol = self.ARM_STYLES[arm_state]
            self.arm.setText(txt)
            self.arm.setStyleSheet(
                f"background: #1e1f22; color: {acol}; border-radius: 4px; "
                f"padding: 1px 6px; font-size: 11px; font-weight: 700;")
            self.arm.setVisible(True)
        else:
            self.arm.setVisible(False)
        txt = f"~{est:,.0f} / {cap:,.0f} m³"
        if eta_s is not None:
            txt += f"  ·  ⏳ {fmt_eta(eta_s)}"
        self.amount.setText(txt)
        self.bar.setValue(int(min(pct, 100) * 10))
        self.bar.setStyleSheet(
            f"QProgressBar::chunk {{ background: {col}; border-radius: 4px; }}")

    def menu(self, pos):
        m = QMenu(self)
        m.addAction("Reset (hold emptied)", lambda: self.main.reset_char(self.name))
        m.addAction("Set current m³…", lambda: self.main.calibrate_char(self.name))
        m.addAction("Set capacity…", lambda: self.main.capacity_char(self.name))
        m.addSeparator()
        m.addAction("Remove from list", lambda: self.main.remove_char(self.name))
        m.exec(self.mapToGlobal(pos))


# ---------------------------------------------------------------------------
# Daily ledger dialog + trend chart
# ---------------------------------------------------------------------------

# Validated categorical palette (dark mode, checked against this app's row
# surface #313338 with the dataviz validator: CVD dE 8.4, normal dE 19.3,
# all-pass; green sits at 2.56:1 so the chart ships direct labels, tooltips
# and the Day-detail table as relief). Assignment is by fixed slot order.
CHART_SERIES = ["#3987e5", "#008300", "#d55181", "#c98500",
                "#199e70", "#d95926", "#9085e9", "#e66767"]
CHART_OTHER = "#6d6f78"
CHART_SURFACE = "#313338"
CHART_GRID = "#3f4147"
CHART_TEXT = "#dbdee1"
CHART_MUTED = "#949ba4"


def knum(v: float) -> str:
    if v >= 1e9:
        return f"{v/1e9:.1f}B"
    if v >= 1e6:
        return f"{v/1e6:.1f}M"
    if v >= 1e3:
        return f"{v/1e3:.0f}k"
    return f"{v:.0f}"


class LedgerChart(QWidget):
    """Stacked bars, one per day, split by character. Custom QPainter -
    no extra dependency, Nuitka-safe, dark-theme native."""

    def __init__(self):
        super().__init__()
        self.days: list[str] = []
        self.series: list[str] = []        # legend order == stack order
        self.values: dict = {}             # day -> {char: value}
        self.labels: list[str] = []
        self.unit = "m³"
        self.setMouseTracking(True)
        self.setMinimumHeight(260)
        self._hit: list[tuple] = []        # (QRect, char, day, value)

    def set_data(self, days, series, values, unit, labels=None):
        self.days, self.series, self.values, self.unit = days, series, values, unit
        self.labels = labels or [d[5:] for d in days]  # default MM.DD
        self.update()

    def color_for(self, char: str) -> str:
        try:
            i = self.series.index(char)
        except ValueError:
            return CHART_OTHER
        return CHART_SERIES[i] if i < len(CHART_SERIES) else CHART_OTHER

    def paintEvent(self, ev):
        from PySide6.QtGui import QPainterPath
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(CHART_SURFACE))
        self._hit = []
        W, H = self.width(), self.height()
        pad_l, pad_r, pad_t, pad_b = 56, 12, 34, 26
        plot_w, plot_h = W - pad_l - pad_r, H - pad_t - pad_b
        f = QFont()
        f.setPixelSize(11)
        p.setFont(f)

        # legend (always present; identity never color-alone: tooltips +
        # the Day-detail table carry names too)
        x = pad_l
        for ch in self.series:
            p.setBrush(QColor(self.color_for(ch)))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(x, 10, 10, 10, 2, 2)
            p.setPen(QColor(CHART_TEXT))
            w = p.fontMetrics().horizontalAdvance(ch)
            p.drawText(x + 14, 19, ch)
            x += 14 + w + 16
        if not self.days:
            p.setPen(QColor(CHART_MUTED))
            p.drawText(self.rect(), Qt.AlignCenter, "No ledger data yet")
            p.end()
            return

        totals = {d: sum(self.values.get(d, {}).values()) for d in self.days}
        vmax = max(totals.values()) or 1.0

        # recessive grid: 3 lines + muted y labels
        p.setPen(QColor(CHART_GRID))
        for i in (1, 2, 3):
            y = pad_t + plot_h - plot_h * i / 3
            p.drawLine(pad_l, int(y), W - pad_r, int(y))
            p.setPen(QColor(CHART_MUTED))
            p.drawText(4, int(y) + 4, knum(vmax * i / 3))
            p.setPen(QColor(CHART_GRID))
        p.drawLine(pad_l, pad_t + plot_h, W - pad_r, pad_t + plot_h)  # baseline

        n = len(self.days)
        slot = plot_w / n
        bar_w = max(6, min(48, int(slot) - 4))
        xstep = max(1, n // 10)  # label every k-th day to avoid collisions
        for i, day in enumerate(self.days):
            bx = int(pad_l + i * slot + (slot - bar_w) / 2)
            y = pad_t + plot_h
            per = self.values.get(day, {})
            segs = [(ch, per.get(ch, 0.0)) for ch in self.series]
            top_y = y
            for ch, v in segs:
                if v <= 0:
                    continue
                h = plot_h * v / vmax
                seg_top = y - h
                r = QRect(bx, int(seg_top), bar_w, max(1, int(h)))
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(self.color_for(ch)))
                p.drawRect(r)
                # 2px surface gap between stacked segments
                p.fillRect(bx, int(seg_top), bar_w, 2, QColor(CHART_SURFACE))
                self._hit.append((r, ch, day, v))
                y = seg_top
                top_y = seg_top
            # 4px rounded cap on the top segment, anchored stack
            if totals[day] > 0:
                p.setBrush(QColor(self.color_for(
                    next((c for c, v in reversed(segs) if v > 0),
                         self.series[0]))))
                path = QPainterPath()
                path.addRoundedRect(bx, int(top_y), bar_w, 6, 3, 3)
                p.drawPath(path)
                # selective direct label: bar total, only when it fits
                if bar_w >= 26:
                    p.setPen(QColor(CHART_MUTED))
                    p.drawText(QRect(bx - 20, int(top_y) - 16, bar_w + 40, 14),
                               Qt.AlignCenter, knum(totals[day]))
            if i % xstep == 0:
                p.setPen(QColor(CHART_MUTED))
                lbl = self.labels[i] if i < len(self.labels) else day[5:]
                p.drawText(QRect(bx - 24, pad_t + plot_h + 4, bar_w + 48, 16),
                           Qt.AlignCenter, lbl)
        p.end()

    def mouseMoveEvent(self, ev):
        from PySide6.QtWidgets import QToolTip
        pos = ev.position().toPoint()
        for r, ch, day, v in self._hit:
            if r.contains(pos):
                QToolTip.showText(ev.globalPosition().toPoint(),
                                  f"{ch}\n{day}: {v:,.0f} {self.unit}", self)
                return
        QToolTip.hideText()

class LedgerDialog(DarkDialog):
    """Per-day, per-character mined ore: units, m³, and ISK (when priced)."""

    def __init__(self, main: "MainWindow"):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("Daily mining ledger")
        from PySide6.QtWidgets import QComboBox, QTreeWidget

        self.day_combo = QComboBox()
        days = sorted(main.engine.ledger["days"], reverse=True)
        for d in days:
            self.day_combo.addItem(d)
        self.day_combo.currentTextChanged.connect(lambda *_: self.populate())

        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Character / Ore", "Units", "m³",
                                   "ISK (compressed, Jita buy)"])
        self.tree.setRootIsDecorated(True)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #949ba4;")

        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)

        # Day detail tab
        day_tab = QWidget()
        dlay = QVBoxLayout(day_tab)
        top = QHBoxLayout()
        top.addWidget(QLabel("Day (EVE/UTC):"))
        top.addWidget(self.day_combo, 1)
        dlay.addLayout(top)
        dlay.addWidget(self.tree, 1)

        # Trend tab: stacked bars over every day in the ledger
        trend_tab = QWidget()
        tlay = QVBoxLayout(trend_tab)
        mrow = QHBoxLayout()
        mrow.addWidget(QLabel("Metric:"))
        self.metric_combo = QComboBox()
        for m in ("m³", "units", "ISK"):
            self.metric_combo.addItem(m)
        self.metric_combo.currentTextChanged.connect(lambda *_: self.update_chart())
        mrow.addWidget(self.metric_combo)
        mrow.addWidget(QLabel("Range:"))
        self.range_combo = QComboBox()
        for lbl, days in (("7 days", 7), ("30 days", 30), ("90 days", 90),
                          ("1 year", 365), ("All", 0)):
            self.range_combo.addItem(lbl, days)
        self.range_combo.setCurrentIndex(1)   # 30 days
        self.range_combo.currentTextChanged.connect(lambda *_: self.update_chart())
        mrow.addWidget(self.range_combo)
        mrow.addWidget(QLabel("Group:"))
        self.group_combo = QComboBox()
        for g in ("Auto", "Day", "Week", "Month"):
            self.group_combo.addItem(g)
        self.group_combo.currentTextChanged.connect(lambda *_: self.update_chart())
        mrow.addWidget(self.group_combo)
        self.trend_note = QLabel("")
        self.trend_note.setStyleSheet("color: #949ba4;")
        mrow.addWidget(self.trend_note, 1)
        tlay.addLayout(mrow)
        self.chart = LedgerChart()
        tlay.addWidget(self.chart, 1)

        from PySide6.QtWidgets import QTabWidget
        tabs = QTabWidget()
        tabs.addTab(day_tab, "Day detail")
        tabs.addTab(trend_tab, "Trend")

        lay = QVBoxLayout(self)
        lay.addWidget(tabs, 1)
        lay.addWidget(self.status)
        lay.addWidget(bb)
        self.resize(680, 520)

        # ISK uses each day's FROZEN price snapshot (the "Compressed <ore>"
        # market price, since raw ore isn't sold) so priced days keep their
        # worth-when-mined. Unpriced past days are backfilled at today's
        # price - which means we need a current price for EVERY ore that
        # appears anywhere in the ledger, not just today's. Fetch whatever's
        # missing from the cache so backfill can value all of them.
        self.frozen = main.engine.ledger.get("prices", {})
        if main.settings["ledger_fetch_prices"]:
            all_ores = {o for day in main.engine.ledger["days"].values()
                        for chars in day.values() for o in chars}
            want = sorted({"Compressed " + o for o in all_ores})
            have = set(main.prices.cached())
            if want and any(n not in have for n in want):
                main.prices.fetch_async(want)
                self.status.setText("Fetching Jita prices from Fuzzwork…")
                self._poll = QTimer(self)
                self._poll.timeout.connect(self._check_fetch)
                self._poll.start(500)
        self.populate()
        self.update_chart()

    def _today_price(self, ore: str):
        key = "Compressed " + ore
        today = time.strftime("%Y.%m.%d", time.gmtime())
        dp = self.frozen.get(today)
        if dp and key in dp:
            return dp[key]
        return self.main.prices.cached().get(key)

    def price_for(self, day: str, ore: str):
        """(price, exact) COMPRESSED Jita buy for (day, ore). exact=True is
        the frozen worth-when-mined; exact=False means the day wasn't priced
        and we backfilled with today's price (if that option is on).
        price is None when we have nothing at all."""
        key = "Compressed " + ore
        dp = self.frozen.get(day)
        if dp and key in dp:
            return dp[key], True
        if day == time.strftime("%Y.%m.%d", time.gmtime()):
            p = self.main.prices.cached().get(key)
            return (p, True) if p else (None, True)
        # past day with no snapshot: backfill at today's price if allowed
        if self.main.settings["ledger_backfill_prices"]:
            p = self._today_price(ore)
            if p:
                return p, False
        return None, True

    @staticmethod
    def _bucket(day: str, mode: str) -> tuple[str, str]:
        """Return (bucket_key, x_label) for a 'YYYY.MM.DD' day."""
        from datetime import date, timedelta
        y, m, d = (int(x) for x in day.split("."))
        if mode == "month":
            return f"{y:04d}.{m:02d}", f"{y % 100:02d}.{m:02d}"
        if mode == "week":
            dt = date(y, m, d)
            start = dt - timedelta(days=dt.weekday())  # Monday
            key = start.isoformat()
            return key, start.strftime("%m.%d")
        return day, day[5:]

    def update_chart(self):
        days_all = sorted(self.main.engine.ledger["days"])
        span_days = int(self.range_combo.currentData())
        if span_days and days_all:
            from datetime import date, timedelta
            y, m, d = (int(x) for x in days_all[-1].split("."))
            cutoff = (date(y, m, d) - timedelta(days=span_days - 1)).strftime(
                "%Y.%m.%d")
            day_keys = [d for d in days_all if d >= cutoff]
        else:
            day_keys = days_all

        grp = self.group_combo.currentText().lower()
        if grp == "auto":
            n = len(day_keys)
            grp = "day" if n <= 31 else ("week" if n <= 183 else "month")

        metric = self.metric_combo.currentText()
        table = self.main.engine.table
        partial = False
        estimated = False

        def value(ore, qty, day):
            nonlocal partial, estimated
            if metric == "units":
                return float(qty)
            if metric == "m³":
                return (table.unit_volume(ore) or 0.0) * qty
            p, exact = self.price_for(day, ore)
            if not p:
                partial = True
                return 0.0
            if not exact:
                estimated = True   # backfilled at today's price
            return p * qty

        # aggregate day -> bucket, summing per character
        raw: dict = {}          # bucket_key -> {char: value}
        labels: dict = {}       # bucket_key -> x label
        order: list = []        # bucket keys in time order
        char_totals: dict = {}
        for day in day_keys:
            bkey, blab = self._bucket(day, grp)
            if bkey not in raw:
                raw[bkey] = {}
                labels[bkey] = blab
                order.append(bkey)
            per = raw[bkey]
            for char, ores_d in self.main.engine.ledger["days"][day].items():
                v = sum(value(o, q, day) for o, q in ores_d.items())
                per[char] = per.get(char, 0.0) + v
                char_totals[char] = char_totals.get(char, 0.0) + v

        # fixed identity: top 8 characters keep their own slot (sorted by
        # name for stability); everyone else folds into "Other"
        top = sorted(sorted(char_totals, key=char_totals.get, reverse=True)[:8],
                     key=str.lower)
        series = top + (["Other"] if len(char_totals) > len(top) else [])
        values = {}
        for bkey in order:
            per = raw[bkey]
            dd = {ch: per.get(ch, 0.0) for ch in top}
            other = sum(v for ch, v in per.items() if ch not in top)
            if other:
                dd["Other"] = other
            values[bkey] = dd
        self.chart.set_data(order, series, values,
                            "ISK" if metric == "ISK" else metric,
                            labels=[labels[k] for k in order])
        grp_note = {"day": "daily", "week": "weekly", "month": "monthly"}[grp]
        note = f"{grp_note} · {len(order)} bars"
        if metric == "ISK":
            note += "  ·  compressed Jita buy"
            if estimated:
                note += "  ·  incl. days at today's price"
            if partial:
                note += "  ·  some days unpriced"
            age = self.main.prices.age_seconds()
            if age is not None and age > 13 * 3600:   # older than the cadence
                hrs = age / 3600
                note += (f"  ·  prices {hrs/24:.0f}d old"
                         if hrs >= 48 else f"  ·  prices {hrs:.0f}h old")
        self.trend_note.setText(note)

    def _check_fetch(self):
        if self.main.prices.busy:
            return
        self._poll.stop()
        # freeze today's snapshot from what we just fetched (compressed keys)
        today = time.strftime("%Y.%m.%d", time.gmtime())
        cached = self.main.prices.cached()
        want = {"Compressed " + o for chars in
                self.main.engine.ledger["days"].get(today, {}).values()
                for o in chars}
        snap = {n: cached[n] for n in want if n in cached}
        if self.main.engine.snapshot_prices(today, snap):
            self.main.engine.save_ledger()
        self.frozen = self.main.engine.ledger.get("prices", {})
        self.status.setText("Price fetch failed: " + self.main.prices.error
                            if self.main.prices.error else "")
        self.populate()
        self.update_chart()

    def populate(self):
        from PySide6.QtWidgets import QTreeWidgetItem
        self.tree.clear()
        day = self.day_combo.currentText()
        data = self.main.engine.ledger["days"].get(day, {})
        table = self.main.engine.table

        est_used = [False]

        def isk(ore, qty):
            p, exact = self.price_for(day, ore)
            if not p:
                return None
            if not exact:
                est_used[0] = True   # backfilled at today's price
            return p * qty

        def fmt_isk(v):
            return f"{v:,.0f}" if v is not None else "-"

        g_units = g_m3 = 0
        g_isk, g_isk_partial = 0.0, False
        for char in sorted(data, key=lambda c: c.lower()):
            ores_d = data[char]
            c_units = sum(ores_d.values())
            c_m3 = sum((table.unit_volume(o) or 0) * q
                       for o, q in ores_d.items())
            vals = [isk(o, q) for o, q in ores_d.items()]
            c_isk = sum(v for v in vals if v is not None)
            partial = any(v is None for v in vals)
            parent = QTreeWidgetItem(
                [char, f"{c_units:,}", f"{c_m3:,.0f}",
                 fmt_isk(c_isk if not partial or c_isk else None)
                 + (" (partial)" if partial and c_isk else "")])
            f = parent.font(0)
            f.setBold(True)
            for col in range(4):
                parent.setFont(col, f)
            for ore in sorted(ores_d):
                q = ores_d[ore]
                m3 = (table.unit_volume(ore) or 0) * q
                parent.addChild(QTreeWidgetItem(
                    ["    " + ore, f"{q:,}", f"{m3:,.0f}",
                     fmt_isk(isk(ore, q))]))
            self.tree.addTopLevelItem(parent)
            parent.setExpanded(True)
            g_units += c_units
            g_m3 += c_m3
            g_isk += c_isk
            g_isk_partial = g_isk_partial or partial
        total = QTreeWidgetItem(
            ["TOTAL", f"{g_units:,}", f"{g_m3:,.0f}",
             (f"{g_isk:,.0f}" + (" (partial)" if g_isk_partial else ""))
             if g_isk else "-"])
        f = total.font(0)
        f.setBold(True)
        for col in range(4):
            total.setFont(col, f)
        self.tree.addTopLevelItem(total)
        for col in range(4):
            self.tree.resizeColumnToContents(col)
        if not data:
            self.status.setText("No mining recorded for this day yet."
                                if day else "No ledger data yet - it fills "
                                "in as your pilots mine.")
        elif est_used[0]:
            self.status.setText("ISK for this day is estimated at today's "
                                "compressed price - it wasn't priced when "
                                "mined.")
        elif not self.main.prices.error:
            self.status.setText("")


# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------

# (ship, base ore hold m3, ore hold at relevant skill V, bonus skill)
# Sources: EVE University wiki, July 2026. Retriever/Mackinaw holds grow
# +5%/level of Mining Barge; Porpoise/Orca +5%/level of Industrial Command
# Ships; Rorqual +5%/level of Capital Industrial Ships. Others are fixed.
SHIP_ORE_HOLDS = [
    ("Venture",   5000,   5000,  ""),
    ("Covetor",   9000,   9000,  ""),
    ("Hulk",      11500,  11500, ""),
    ("Prospect",  12500,  12500, ""),
    ("Procurer",  16000,  16000, ""),
    ("Skiff",     18500,  18500, ""),
    ("Endurance", 19000,  19000, ""),
    ("Retriever", 27500,  34375, "Mining Barge V"),
    ("Mackinaw",  28000,  35000, "Mining Barge V"),
    ("Porpoise",  50000,  62500, "Industrial Command Ships V"),
    ("Orca",      150000, 187500, "Industrial Command Ships V"),
    ("Rorqual",   300000, 375000, "Capital Industrial Ships V"),
]


class OreHoldInfoDialog(DarkDialog):
    """Reference table of standard ore hold sizes; pick one to use it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Standard ore hold sizes")
        self.chosen: float | None = None
        from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem
        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Ship", "Base m³", "Max-skill m³"])
        self.tree.setRootIsDecorated(False)
        orca_item = None
        for ship, base, at_v, skill in SHIP_ORE_HOLDS:
            it = QTreeWidgetItem([ship, f"{base:,}", f"{at_v:,}"])
            if skill:
                it.setToolTip(2, f"with {skill}")
            self.tree.addTopLevelItem(it)
            if ship == "Orca":
                orca_item = it
        for col in range(3):
            self.tree.resizeColumnToContents(col)
        if orca_item:
            self.tree.setCurrentItem(orca_item)  # Orca is the default pick
        self.tree.itemDoubleClicked.connect(lambda *_: self.use_selected())

        note = QLabel("Hold sizes grow +5%/level from Mining Barge "
                      "(Retriever, Mackinaw), Industrial Command Ships "
                      "(Porpoise, Orca) or Capital Industrial Ships "
                      "(Rorqual). Max-skill column assumes level V - an "
                      "Orca at ICS IV is 180,000 m³.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #949ba4;")

        bb = QDialogButtonBox(QDialogButtonBox.Cancel)
        use = bb.addButton("Use max-skill size", QDialogButtonBox.AcceptRole)
        use_base = bb.addButton("Use base size", QDialogButtonBox.ActionRole)
        bb.rejected.connect(self.reject)
        use.clicked.connect(self.use_selected)
        use_base.clicked.connect(lambda: self.use_selected(base=True))

        lay = QVBoxLayout(self)
        lay.addWidget(self.tree, 1)
        lay.addWidget(note)
        lay.addWidget(bb)
        self.resize(430, 420)

    def use_selected(self, base: bool = False):
        it = self.tree.currentItem()
        if it:
            self.chosen = float(it.text(1 if base else 2).replace(",", ""))
            self.accept()

class SettingsDialog(DarkDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.s = settings

        self.log_dir = QLineEdit(str(settings["log_dir"] or ""))
        self.log_dir.setPlaceholderText("(auto-detect)")
        browse = QPushButton("…")
        browse.setFixedWidth(30)
        browse.clicked.connect(self.pick_dir)
        row = QHBoxLayout()
        row.addWidget(self.log_dir)
        row.addWidget(browse)

        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(1, 100)
        self.threshold.setSuffix(" %")
        self.threshold.setValue(float(settings["threshold_pct"]))

        self.capacity = QDoubleSpinBox()
        self.capacity.setRange(1, 10_000_000)
        self.capacity.setDecimals(0)
        self.capacity.setSuffix(" m³")
        self.capacity.setValue(float(settings["default_capacity"]))
        cap_info = QPushButton("ℹ")
        cap_info.setFixedWidth(30)
        cap_info.setToolTip("Standard ore hold sizes per ship")
        cap_info.clicked.connect(self.show_hold_sizes)
        cap_row = QHBoxLayout()
        cap_row.addWidget(self.capacity)
        cap_row.addWidget(cap_info)

        # alert methods
        self.interval = QDoubleSpinBox()
        self.interval.setRange(0, 1440)
        self.interval.setDecimals(1)
        self.interval.setSuffix(" min")
        self.interval.setToolTip("At most one alert per this many minutes "
                                 "(0 = alert on every crossing)")
        self.interval.setValue(float(settings["alert_interval_min"]))

        self.idle_on = QCheckBox("Alert when a pilot stops receiving ore ticks")
        self.idle_on.setChecked(bool(settings["idle_alert_enabled"]))
        self.idle_min = QDoubleSpinBox()
        self.idle_min.setRange(1, 240)
        self.idle_min.setDecimals(1)
        self.idle_min.setSuffix(" min")
        self.idle_min.setValue(float(settings["idle_alert_min"]))

        self.combat_on = QCheckBox("Alert when a pilot is attacked by a PLAYER "
                                   "(NPC rats never alert)")
        self.combat_on.setChecked(bool(settings["combat_alert_enabled"]))
        self.combat_cd = QDoubleSpinBox()
        self.combat_cd.setRange(10, 3600)
        self.combat_cd.setDecimals(0)
        self.combat_cd.setSuffix(" s cooldown / pilot")
        self.combat_cd.setValue(float(settings["combat_alert_cooldown_s"]))
        self.drone_on = QCheckBox("Alert when a mining drone stops "
                                  "(asteroid depleted)")
        self.drone_on.setChecked(bool(settings["drone_alert_enabled"]))

        self.m_popup = QCheckBox("Pop-up notification (Windows toast)")
        self.m_popup.setChecked(bool(settings["notify_popup"]))
        self.m_overlay = QCheckBox("On-screen overlay banner alert")
        self.m_overlay.setChecked(bool(settings["notify_overlay"]))
        self.m_sound = QCheckBox("Sound (system ding)")
        self.m_sound.setChecked(bool(settings["notify_sound"]))
        self.m_webhook = QCheckBox("Webhook (Discord webhook URL or any HTTP endpoint)")
        self.m_webhook.setChecked(bool(settings["notify_webhook"]))
        self.webhook_url = QLineEdit(str(settings["webhook_url"]))
        self.webhook_url.setPlaceholderText("https://discord.com/api/webhooks/…")
        from PySide6.QtWidgets import QComboBox
        self.mention = QComboBox()
        self.mention.addItem("@everyone", "everyone")
        self.mention.addItem("Specific user / role / @here", "custom")
        self.mention.addItem("No ping (embed only)", "none")
        from PySide6.QtGui import QBrush, QPalette
        from PySide6.QtWidgets import QStyledItemDelegate
        # the default combo delegate ignores QSS ::item rules; this one obeys
        self.mention.setItemDelegate(QStyledItemDelegate(self.mention))
        for i in range(self.mention.count()):  # some styles ignore popup QSS
            self.mention.setItemData(i, QBrush(QColor("#dbdee1")),
                                     Qt.ForegroundRole)
            self.mention.setItemData(i, QBrush(QColor("#1e1f22")),
                                     Qt.BackgroundRole)
        view = self.mention.view()
        pal = view.palette()
        pal.setColor(QPalette.Text, QColor("#dbdee1"))
        pal.setColor(QPalette.Base, QColor("#1e1f22"))
        pal.setColor(QPalette.Highlight, QColor("#5865f2"))
        pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
        view.setPalette(pal)
        idx = self.mention.findData(str(settings["discord_mention"]))
        self.mention.setCurrentIndex(max(0, idx))
        self.mention_id = QLineEdit(str(settings["discord_mention_id"]))
        self.mention_id.setPlaceholderText(
            "user ID (Developer Mode > Copy User ID), @here, or <@&roleID>")
        self.m_ntfy = QCheckBox("Phone push via ntfy.sh (free app, no account)")
        self.m_ntfy.setChecked(bool(settings["notify_ntfy"]))
        self.ntfy_topic = QLineEdit(str(settings["ntfy_topic"]))
        self.ntfy_topic.setPlaceholderText("your-secret-topic-name")
        self.test_btn = QPushButton("Send test alert")

        # downtime auto-close
        self.dt_close = QCheckBox("Force-close all EVE clients before daily "
                                  "downtime (11:00 UTC cluster shutdown)")
        self.dt_close.setChecked(bool(settings["close_before_downtime"]))
        self.dt_lead = QDoubleSpinBox()
        self.dt_lead.setRange(0.5, 120)
        self.dt_lead.setDecimals(1)
        self.dt_lead.setSuffix(" min before")
        self.dt_lead.setValue(float(settings["close_minutes_before"]))

        self.ontop = QCheckBox("Keep main window always on top of other windows")
        self.ontop.setChecked(bool(settings["always_on_top"]))
        self.comp_out = QCheckBox("Compressed ore is moved out of the ore hold\n"
                                  "(compression frees the full raw volume)")
        self.comp_out.setChecked(bool(settings["compressed_leaves_hold"]))
        self.cwatch = QCheckBox("Detect closed clients via window titles "
                                "('EVE - Name', read-only) - closed pilots "
                                "never fire the idle alert")
        self.cwatch.setChecked(bool(settings["client_watch_enabled"]))
        self.ledger_on = QCheckBox("Track daily mined ore per character (📒 ledger)")
        self.ledger_on.setChecked(bool(settings["ledger_enabled"]))
        self.ledger_prices = QCheckBox("Fetch Jita prices for ISK values "
                                       "(compressed, Fuzzwork.co.uk, cached 12 h)")
        self.ledger_prices.setChecked(bool(settings["ledger_fetch_prices"]))
        self.ledger_backfill = QCheckBox("Value unpriced past days at today's "
                                         "price (marked as estimated)")
        self.ledger_backfill.setChecked(bool(settings["ledger_backfill_prices"]))
        self.upd_check = QCheckBox("Check GitHub for app updates (daily)")
        self.upd_check.setChecked(bool(settings["update_check"]))
        self.upd_repo = QLineEdit(str(settings["update_repo"]))
        self.upd_repo.setPlaceholderText("owner/repo  (e.g. jrod/ore-hold-watcher)")

        self.dbg = QCheckBox("Debug logging to debug.log (verbose; off = "
                             "no log file is written)")
        self.dbg.setChecked(bool(settings["debug_verbose"]))
        self.open_log = QPushButton("Open debug log")
        self.open_log.clicked.connect(
            lambda: os.startfile(config_dir() / "debug.log")
            if sys.platform == "win32" and (config_dir() / "debug.log").exists()
            else None)

        from PySide6.QtWidgets import QTabWidget
        tabs = QTabWidget()

        gen = QWidget()
        gf = QFormLayout(gen)
        gf.addRow("Gamelogs folder:", row)
        gf.addRow("Alert threshold:", self.threshold)
        gf.addRow("Default ore hold capacity:", cap_row)
        gf.addRow(self.ontop)
        gf.addRow(self.comp_out)
        gf.addRow(self.cwatch)
        gf.addRow(self.ledger_on)
        gf.addRow(self.ledger_prices)
        gf.addRow(self.ledger_backfill)
        gf.addRow(self.upd_check)
        gf.addRow("GitHub repo:", self.upd_repo)
        gf.addRow(self.dbg)
        gf.addRow(self.open_log)
        tabs.addTab(gen, "General")

        al = QWidget()
        af = QFormLayout(al)
        af.addRow("Min. time between alerts:", self.interval)
        af.addRow(self.idle_on)
        af.addRow("Idle after:", self.idle_min)
        af.addRow(self.combat_on)
        af.addRow("Combat re-alert:", self.combat_cd)
        af.addRow(self.drone_on)
        af.addRow(self.m_popup)
        af.addRow(self.m_overlay)
        af.addRow(self.m_sound)
        af.addRow(self.m_webhook)
        af.addRow("Webhook URL:", self.webhook_url)
        af.addRow("Discord ping:", self.mention)
        af.addRow("Ping target:", self.mention_id)
        af.addRow(self.m_ntfy)
        af.addRow("ntfy topic:", self.ntfy_topic)
        af.addRow(self.test_btn)
        tabs.addTab(al, "Alerts")

        dt = QWidget()
        df = QFormLayout(dt)
        df.addRow(self.dt_close)
        df.addRow("Close clients:", self.dt_lead)
        note = QLabel("EVE's daily cluster shutdown is 11:00 UTC. When "
                      "enabled, all EVE client processes are force-closed "
                      "this many minutes beforehand (once per day, only "
                      "while this app is running).")
        note.setWordWrap(True)
        note.setStyleSheet("color: #949ba4;")
        df.addRow(note)
        tabs.addTab(dt, "Downtime")

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addWidget(tabs)
        root.addWidget(bb)
        self.setMinimumWidth(520)

    def show_hold_sizes(self):
        dlg = OreHoldInfoDialog(self)
        if dlg.exec() == QDialog.Accepted and dlg.chosen:
            self.capacity.setValue(dlg.chosen)

    def pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Select Gamelogs folder",
                                             self.log_dir.text())
        if d:
            self.log_dir.setText(d)

    def apply(self):
        self.s["log_dir"] = self.log_dir.text()
        self.s["threshold_pct"] = self.threshold.value()
        self.s["default_capacity"] = self.capacity.value()
        self.s["alert_interval_min"] = self.interval.value()
        self.s["idle_alert_enabled"] = self.idle_on.isChecked()
        self.s["idle_alert_min"] = self.idle_min.value()
        self.s["combat_alert_enabled"] = self.combat_on.isChecked()
        self.s["combat_alert_cooldown_s"] = self.combat_cd.value()
        self.s["drone_alert_enabled"] = self.drone_on.isChecked()
        self.s["close_before_downtime"] = self.dt_close.isChecked()
        self.s["close_minutes_before"] = self.dt_lead.value()
        self.s["notify_popup"] = self.m_popup.isChecked()
        self.s["notify_overlay"] = self.m_overlay.isChecked()
        self.s["notify_sound"] = self.m_sound.isChecked()
        self.s["notify_webhook"] = self.m_webhook.isChecked()
        self.s["webhook_url"] = self.webhook_url.text().strip()
        self.s["discord_mention"] = self.mention.currentData()
        self.s["discord_mention_id"] = self.mention_id.text().strip()
        self.s["notify_ntfy"] = self.m_ntfy.isChecked()
        self.s["ntfy_topic"] = self.ntfy_topic.text().strip()
        self.s["always_on_top"] = self.ontop.isChecked()
        self.s["compressed_leaves_hold"] = self.comp_out.isChecked()
        self.s["client_watch_enabled"] = self.cwatch.isChecked()
        self.s["ledger_enabled"] = self.ledger_on.isChecked()
        self.s["ledger_fetch_prices"] = self.ledger_prices.isChecked()
        self.s["ledger_backfill_prices"] = self.ledger_backfill.isChecked()
        self.s["update_check"] = self.upd_check.isChecked()
        self.s["update_repo"] = self.upd_repo.text().strip()
        self.s["debug_verbose"] = self.dbg.isChecked()
        logging.getLogger("orewatcher").setLevel(
            logging.DEBUG if self.dbg.isChecked() else logging.INFO)
        set_file_logging(self.dbg.isChecked())
        self.s.save()
        log.info("settings saved to %s: always_on_top=%s overlay=%s popup=%s "
                 "sound=%s webhook=%s ntfy=%s interval=%.1fmin",
                 self.s.path, self.s["always_on_top"], self.s["notify_overlay"],
                 self.s["notify_popup"], self.s["notify_sound"],
                 self.s["notify_webhook"], self.s["notify_ntfy"],
                 float(self.s["alert_interval_min"]))


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = Settings()
        self.warned_ores: set[str] = set()

        # resolve the watch folder: explicit setting if it exists,
        # otherwise auto-detect for the active user
        configured = str(self.settings["log_dir"] or "").strip()
        watch_dir = None
        if configured:
            p = Path(configured)
            if p.is_dir() and any(p.glob("*.txt")):
                watch_dir = p
            else:
                log.warning("configured log_dir missing or has no logs (%s); "
                            "auto-detecting", configured)
        if watch_dir is None:
            watch_dir = detect_log_dir()

        self.engine = Engine(
            log_dir=watch_dir,
            state_path=config_dir() / "state.json",
            ore_override_path=config_dir() / "ores_override.json",
            mining_patterns=self.settings["mining_patterns"] or None,
            lookback_hours=float(self.settings["lookback_hours"]),
            default_capacity=float(self.settings["default_capacity"]),
            compressed_leaves_hold=bool(self.settings["compressed_leaves_hold"]),
            combat_enabled=bool(self.settings["combat_alert_enabled"]),
            ledger_path=config_dir() / "ledger.json",
            ledger_enabled=bool(self.settings["ledger_enabled"]),
        )
        self.engine.drone_enabled = bool(self.settings["drone_alert_enabled"])
        self.prices = PriceService()
        self.clients = ClientWatcher(self.settings["eve_process_names"])
        self._last_client_scan = 0.0
        self._last_price_check = 0.0

        self.setWindowTitle(APP_NAME)
        try:
            w_px, h_px = self.settings["window_size"]
            self.resize(max(460, int(w_px)), max(320, int(h_px)))
        except Exception:
            self.resize(560, 500)
        self.apply_on_top()

        central = QWidget()
        v = QVBoxLayout(central)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(8)

        hdr = QHBoxLayout()
        title = QLabel("⛏  Fleet Ore Holds")
        title.setStyleSheet("font-size: 15px; font-weight: 700;")
        hdr.addWidget(title)
        hdr.addStretch(1)
        b_ledger = QPushButton("📒")
        b_ledger.setFixedWidth(34)
        b_ledger.setToolTip("Daily mining ledger")
        b_ledger.clicked.connect(lambda: LedgerDialog(self).exec())
        b_reset = QPushButton("Recalculate")
        b_reset.setToolTip("Rebuild all estimates by replaying the logs "
                           "(last %d h)" % int(float(self.settings["lookback_hours"])))
        b_reset.clicked.connect(self.recalculate)
        b_cfg = QPushButton("⚙")
        b_cfg.setFixedWidth(34)
        b_cfg.clicked.connect(self.open_settings)
        hdr.addWidget(b_ledger)
        hdr.addWidget(b_reset)
        hdr.addWidget(b_cfg)
        v.addLayout(hdr)

        self.status = QLabel("")
        self.status.setObjectName("amount")
        self.status.setWordWrap(True)
        v.addWidget(self.status)

        self.rows_box = QVBoxLayout()
        self.rows_box.setSpacing(6)
        wrap = QWidget()
        outer = QVBoxLayout(wrap)
        outer.addLayout(self.rows_box)
        outer.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(wrap)
        v.addWidget(scroll, 1)

        self.setCentralWidget(central)
        self.rows: dict[str, tuple[QWidget, CharRow]] = {}

        # Tray
        self.tray = QSystemTrayIcon(make_tray_icon(0), self)
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        menu.addAction("Show / Hide", self.toggle_visible)
        menu.addAction("Recalculate from logs", self.recalculate)
        menu.addAction("Reset all holds to 0", self.reset_all)
        menu.addAction("Check for updates", self.manual_update_check)
        menu.addSeparator()
        if sys.platform == "win32":
            menu.addAction("Open debug log",
                           lambda: os.startfile(config_dir() / "debug.log")
                           if (config_dir() / "debug.log").exists() else None)
        menu.addAction("Quit", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self.toggle_visible() if r == QSystemTrayIcon.Trigger else None)
        self.tray.show()

        self.notifier = Notifier(self.settings, self.tray)
        self.updater = Updater(self.settings)
        self._update_prompted: set[str] = set()
        self._last_update_check = 0.0
        if self.settings["update_check"] and self.updater.can_update():
            QTimer.singleShot(20_000, lambda: self.updater.check_async())
            self._last_update_check = time.time()
        self._last_alert_ts = 0.0     # rate limiter for threshold alerts
        self._combat_alerted: dict[str, float] = {}  # per-pilot combat cooldown
        self._drone_alerted: dict[str, float] = {}   # per-pilot drone cooldown
        self._alert_pending = False
        self._pending_title = ""

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(int(float(self.settings["poll_seconds"]) * 1000))
        self.tick()

    # -- helpers -------------------------------------------------------------
    def apply_on_top(self):
        want = bool(self.settings["always_on_top"])
        if bool(self.windowFlags() & Qt.WindowStaysOnTopHint) != want:
            was_visible = self.isVisible()
            self.setWindowFlag(Qt.WindowStaysOnTopHint, want)
            if was_visible:
                self.show()  # setWindowFlag hides the window; re-show it
        # belt & braces: force the native WS_EX_TOPMOST bit to match, in
        # case anything else (or a stale flag) left the window pinned
        if self.isVisible() and sys.platform == "win32":
            try:
                import ctypes
                HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
                SWP_NOMOVE_NOSIZE_NOACTIVATE = 0x0002 | 0x0001 | 0x0010
                ctypes.windll.user32.SetWindowPos(
                    int(self.winId()),
                    HWND_TOPMOST if want else HWND_NOTOPMOST,
                    0, 0, 0, 0, SWP_NOMOVE_NOSIZE_NOACTIVATE)
            except Exception as e:
                log.warning("native topmost enforce failed: %s", e)
        log.info("always_on_top=%s (qt flag=%s)", want,
                 bool(self.windowFlags() & Qt.WindowStaysOnTopHint))

    def showEvent(self, ev):
        super().showEvent(ev)
        style_titlebar(self)
        # re-assert whenever the window (re)appears, e.g. from the tray
        QTimer.singleShot(0, self.apply_on_top)

    def toggle_visible(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()
            self.activateWindow()

    def closeEvent(self, ev):  # close -> minimize to tray
        self.settings["window_size"] = [self.width(), self.height()]
        self.settings.save()
        ev.ignore()
        self.hide()
        self.tray.showMessage(APP_NAME, "Still watching in the tray. "
                              "Right-click the icon to quit.",
                              QSystemTrayIcon.Information, 2500)

    def quit(self):
        self.engine.save_state()
        self.tray.hide()
        QApplication.quit()

    # -- notifications --------------------------------------------------------
    def notify(self, title: str, body: str, payload: dict | None = None):
        self.notifier.alert(title, body, payload)

    def fleet_summary(self) -> tuple[str, dict]:
        """All characters' current state, fullest first."""
        chars = sorted(self.engine.chars.values(),
                       key=lambda c: c.pct, reverse=True)
        lines, payload_chars = [], []
        for c in chars:
            eta = c.eta_full_s()
            eta_txt = f" · full in {fmt_eta(eta)}" if eta else ""
            lines.append(f"{c.name} - ~{c.est_m3:,.0f} / {c.capacity:,.0f} m³ "
                         f"({c.pct:.1f}%){eta_txt}")
            payload_chars.append(
                {"character": c.name, "est_m3": round(c.est_m3),
                 "capacity_m3": round(c.capacity), "pct": round(c.pct, 1),
                 "eta_min": round(eta / 60) if eta is not None else None})
        return ("\n".join(lines) or "No characters tracked yet.",
                {"characters": payload_chars})

    def request_alert(self, title: str):
        """Queue a threshold alert; sent as a full-fleet digest, at most one
        per alert_interval_min minutes (a suppressed alert is sent as soon
        as the interval expires)."""
        self._pending_title = title
        self._alert_pending = True
        self._flush_alert()

    def _flush_alert(self):
        if not self._alert_pending:
            return
        interval = max(0.0, float(self.settings["alert_interval_min"])) * 60.0
        now = time.time()
        if now - self._last_alert_ts < interval:
            return  # rate-limited; tick() retries until the window opens
        self._last_alert_ts = now
        self._alert_pending = False
        body, payload = self.fleet_summary()
        self.notifier.alert(self._pending_title, body, payload)

    # -- downtime auto-close ---------------------------------------------------
    def _check_downtime_close(self):
        if not self.settings["close_before_downtime"]:
            return
        try:
            hh, mm = str(self.settings["downtime_utc"]).split(":")
            from datetime import datetime, timedelta, timezone
            now = datetime.now(timezone.utc)
            dt_today = now.replace(hour=int(hh), minute=int(mm),
                                   second=0, microsecond=0)
            shutdown = dt_today if now <= dt_today else dt_today + timedelta(days=1)
            lead = timedelta(minutes=max(0.5, float(
                self.settings["close_minutes_before"])))
            in_window = shutdown - lead <= now < shutdown
        except Exception as e:
            log.warning("downtime check failed: %s", e)
            return
        today_key = shutdown.strftime("%Y-%m-%d")
        if not in_window or getattr(self, "_closed_for", None) == today_key:
            return
        self._closed_for = today_key
        killed = self._force_close_eve()
        self.notifier.alert(
            "⏻ EVE downtime in <" + f"{lead.seconds // 60} min - clients closed",
            f"Force-closed {killed} process(es) (EVE clients plus their "
            f"child processes) ahead of the {self.settings['downtime_utc']} "
            f"UTC cluster shutdown.",
            {"event": "downtime_close", "processes_killed": killed})

    def _force_close_eve(self) -> int:
        """taskkill /F every configured EVE client process. Returns count."""
        import subprocess
        names = self.settings["eve_process_names"] or ["exefile.exe"]
        killed = 0
        for name in names:
            try:
                if sys.platform == "win32":
                    # matches the community-standard downtime scheduled task:
                    # taskkill /f /t /im exefile.exe (/T kills child processes)
                    r = subprocess.run(
                        ["taskkill", "/f", "/t", "/im", name],
                        capture_output=True, text=True, timeout=30,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    out = (r.stdout or "") + (r.stderr or "")
                    killed += out.upper().count("SUCCESS")
                    log.info("taskkill %s -> rc=%s %s", name, r.returncode,
                             out.strip().replace("\n", " | "))
                else:
                    r = subprocess.run(["pkill", "-9", "-f", name],
                                       capture_output=True, timeout=30)
                    killed += 1 if r.returncode == 0 else 0
            except Exception as e:
                log.warning("force close %s failed: %s", name, e)
        return killed

    # -- actions ---------------------------------------------------------------
    def reset_all(self):
        self.engine.reset_all()
        self.refresh()

    def recalculate(self):
        self.engine.recalculate()
        self.refresh()

    def reset_char(self, name: str):
        self.engine.reset(name)
        self.refresh()

    def calibrate_char(self, name: str):
        c = self.engine.char(name)
        val, ok = QInputDialog.getDouble(
            self, "Set current amount",
            f"Current ore in {name}'s hold (m³):", c.est_m3, 0,
            10_000_000, 0)
        if ok:
            self.engine.calibrate(name, val)
            self.refresh()

    def capacity_char(self, name: str):
        c = self.engine.char(name)
        val, ok = QInputDialog.getDouble(
            self, "Set capacity",
            f"Ore hold capacity for {name} (m³):", c.capacity, 1,
            10_000_000, 0)
        if ok:
            self.engine.set_capacity(name, val)
            self.refresh()

    def remove_char(self, name: str):
        self.engine.remove(name)
        self.refresh()

    def open_settings(self):
        dlg = SettingsDialog(self.settings, self)

        def send_test():
            dlg.apply()  # so the test uses what's currently ticked/typed
            body, payload = self.fleet_summary()  # real current fleet state
            self.notifier.alert("⚠ Test alert - Ore Hold Watcher", body, payload)
        dlg.test_btn.clicked.connect(send_test)

        if dlg.exec() == QDialog.Accepted:
            dlg.apply()
            configured = str(self.settings["log_dir"] or "").strip()
            self.engine.log_dir = (Path(configured) if configured and
                                   Path(configured).is_dir() else detect_log_dir())
            self.engine.default_capacity = float(self.settings["default_capacity"])
            self.engine.compressed_leaves_hold = bool(
                self.settings["compressed_leaves_hold"])
            self.engine.combat_enabled = bool(
                self.settings["combat_alert_enabled"])
            self.engine.drone_enabled = bool(
                self.settings["drone_alert_enabled"])
            self.engine.ledger_enabled = bool(
                self.settings["ledger_enabled"])
            self.apply_on_top()
            self.show()
            self.refresh()

    # -- daily ISK price snapshot ------------------------------------------------
    def _maintain_prices(self):
        """Keep today's frozen price basis fresh so each day's ISK is
        captured at the prices in effect while it was mined. Values ore at
        its COMPRESSED Jita price (raw ore isn't sold). Only runs when the
        ledger and price fetching are on."""
        if not (self.settings["ledger_enabled"] and
                self.settings["ledger_fetch_prices"]):
            return
        today = time.strftime("%Y.%m.%d", time.gmtime())
        ores = {o for chars in
                self.engine.ledger["days"].get(today, {}).values()
                for o in chars}
        if not ores:
            return
        # value compressed: mined units compress 1:1, so ISK uses the
        # "Compressed <ore>" market price
        want = sorted({"Compressed " + o for o in ores})
        cached = self.prices.cached()
        missing = [n for n in want if n not in cached]
        now = time.time()
        # fetch when prices are stale OR a mined ore has no price yet (e.g.
        # you switched to a new ore mid-day); short cooldown so a new ore's
        # ISK shows within minutes instead of waiting out the 12 h window
        if ((self.prices.stale() or missing) and
                now - self._last_price_check > 600):
            self._last_price_check = now
            self.prices.fetch_async(want)   # background; snapshot next tick
            cached = self.prices.cached()
        snap = {n: cached[n] for n in want if n in cached}
        if snap and self.engine.snapshot_prices(today, snap):
            self.engine.save_ledger()

    # -- updates -----------------------------------------------------------------
    def manual_update_check(self):
        if not self.updater.repo():
            QMessageBox.information(
                self, "Updates", "Set the GitHub repo (owner/repo) in "
                "Settings > General first.")
            return
        if not is_frozen():
            QMessageBox.information(
                self, "Updates", "Running from source - update by pulling "
                "the repo. Auto-update only applies to the built exe.")
            return
        self.updater.check_async(manual=True)

    def _pump_updates(self):
        u = self.updater
        if u.error:
            err, u.error = u.error, None
            if u.manual:
                QMessageBox.warning(self, "Update check failed", err)
        if u.up_to_date:
            tag, u.up_to_date = u.up_to_date, None
            if u.manual:
                QMessageBox.information(
                    self, "Updates",
                    f"You're on the latest version ({tag}).")
        if u.available:
            info, u.available = u.available, None
            if info["version"] not in self._update_prompted:
                self._update_prompted.add(info["version"])
                ans = QMessageBox.question(
                    self, "Update available",
                    f"Version {info['version']} is available "
                    f"(you have {info['current']}).\n\n"
                    "Download and restart to update now?",
                    QMessageBox.Yes | QMessageBox.No)
                if ans == QMessageBox.Yes:
                    u.download_async(info["url"])
        if u.downloaded:
            if u.apply():          # swap script waits for our exit
                self.quit()
            else:
                u.downloaded = None
        # daily re-check
        if (self.settings["update_check"] and u.can_update() and
                time.time() - self._last_update_check > 86_400):
            self._last_update_check = time.time()
            u.check_async()

    # -- main loop --------------------------------------------------------------
    def tick(self):
        events = self.engine.poll()
        threshold = float(self.settings["threshold_pct"])
        rearm = threshold - float(self.settings["rearm_margin_pct"])
        idle_after = max(60.0, float(self.settings["idle_alert_min"]) * 60.0)
        now_utc = time.time()
        # window-title scan every 10 s: which characters have a live client
        watch = bool(self.settings["client_watch_enabled"])
        if watch and now_utc - self._last_client_scan > 10:
            self._last_client_scan = now_utc
            self.clients.refresh()
        watch = watch and self.clients.ready

        def is_closed(name: str) -> bool:
            return watch and name not in self.clients.online
        for ev in events:
            # a LIVE mining tick (not startup replay of old lines) arms the
            # idle alert for that pilot
            if (isinstance(ev, MiningEvent) and
                    now_utc - ts_to_epoch(ev.ts) < idle_after):
                self.engine.char(ev.character).idle_notified = False
            # PLAYER aggression: urgent, bypasses the digest rate limiter.
            # NPC rats (is_player=False) never alert. The 2-minute liveness
            # guard keeps startup replay of old fights silent.
            if (isinstance(ev, CombatEvent) and ev.is_player and
                    self.settings["combat_alert_enabled"] and
                    ev.character in self.engine.chars and   # tracked miners only
                    now_utc - ts_to_epoch(ev.ts) < 120):
                cd = float(self.settings["combat_alert_cooldown_s"])
                last = self._combat_alerted.get(ev.character, 0.0)
                if now_utc - last >= cd:
                    self._combat_alerted[ev.character] = now_utc
                    body, payload = self.fleet_summary()
                    payload["event"] = "under_attack"
                    payload["attacker"] = ev.attacker
                    self.notifier.alert(
                        f"🚨 {ev.character} UNDER ATTACK - {ev.attacker}",
                        f"{ev.kind}\n{body}", payload)
            # mining drone stopped (asteroid depleted); debounced so a whole
            # flight returning on a dry rock is one alert. Tracked miners only.
            if (isinstance(ev, DroneStopEvent) and
                    self.settings["drone_alert_enabled"] and
                    ev.character in self.engine.chars and
                    now_utc - ts_to_epoch(ev.ts) < 120):
                cd = float(self.settings["drone_alert_cooldown_s"])
                if now_utc - self._drone_alerted.get(ev.character, 0.0) >= cd:
                    self._drone_alerted[ev.character] = now_utc
                    self.request_alert(
                        f"🛑 {ev.character} - mining drone(s) stopped "
                        f"(asteroid depleted)")
            if isinstance(ev, HoldFullEvent):
                c = self.engine.char(ev.character)
                if not c.notified:
                    c.notified = True
                    self.request_alert(f"⚠ {ev.character} - ore hold FULL")
            elif isinstance(ev, UnknownOreEvent):
                if ev.ore not in self.warned_ores:
                    self.warned_ores.add(ev.ore)
                    self.notify("Unknown ore type",
                                f"'{ev.ore}' isn't in the volume table - add it to "
                                f"ores_override.json (Settings folder) so it counts.")
        # threshold crossings / re-arm
        for c in self.engine.chars.values():
            if c.pct >= threshold and not c.notified:
                c.notified = True
                self.request_alert(f"⚠ {c.name} - {c.pct:.1f}% full")
            elif c.pct < rearm and c.notified:
                c.notified = False
        # idle detection: armed pilots whose ticks stopped for idle_after.
        # A CLOSED client is not idle: it disarms silently and never fires
        # the idle alert (re-arms automatically on the next live tick).
        if self.settings["idle_alert_enabled"]:
            for c in self.engine.chars.values():
                if is_closed(c.name):
                    c.idle_notified = True
                    continue
                if c.idle_notified or not c.rate_events:
                    continue
                gap = now_utc - c.rate_events[-1][0]
                if gap >= idle_after:
                    c.idle_notified = True   # fire once until mining resumes
                    self.request_alert(
                        f"⏸ {c.name} - no ore ticks for {int(gap // 60)} min")
        self._flush_alert()          # send any alert the rate limiter held back
        self._check_downtime_close()
        self._maintain_prices()
        self._pump_updates()
        self.refresh()

    def refresh(self):
        chars = sorted(self.engine.chars.values(),
                       key=lambda c: c.pct, reverse=True)
        wanted = [c.name for c in chars]
        # drop rows for removed chars
        for name in list(self.rows):
            if name not in wanted:
                frame, _ = self.rows.pop(name)
                frame.setParent(None)
                frame.deleteLater()
        # (re)build rows in order
        for i, c in enumerate(chars):
            if c.name not in self.rows:
                from PySide6.QtWidgets import QFrame
                frame = QFrame()
                frame.setObjectName("row")
                lay = QVBoxLayout(frame)
                lay.setContentsMargins(0, 0, 0, 0)
                row = CharRow(self, c.name)
                lay.addWidget(row)
                self.rows[c.name] = (frame, row)
            frame, row = self.rows[c.name]
            self.rows_box.removeWidget(frame)
            self.rows_box.insertWidget(i, frame)
            closed = (bool(self.settings["client_watch_enabled"]) and
                      self.clients.ready and c.name not in self.clients.online)
            if closed:
                arm = "closed"
            elif not self.settings["idle_alert_enabled"]:
                arm = None
            elif not c.idle_notified:
                arm = "armed"
            elif c.rate_events:
                arm = "idle"
            else:
                arm = "standby"
            row.update_state(c.est_m3, c.capacity, c.eta_full_s(), arm)

        # who fills up first at current mining rates?
        etas = [(c.eta_full_s(), c) for c in chars]
        etas = [(e, c) for e, c in etas if e is not None and e > 0]
        first_full = min(etas, key=lambda t: t[0]) if etas else None

        s = self.engine.stats
        dir_ok = self.engine.log_dir.is_dir()
        parts = []
        if first_full:
            parts.append(f"⏳ First hold full: {first_full[1].name} in "
                         f"~{fmt_eta(first_full[0])}")
        parts += [f"{'Watching' if dir_ok else '⚠ MISSING FOLDER'}: "
                 f"{self.engine.log_dir}",
                 f"{len(self.engine.files)} log files · "
                 f"{s['lines']:,} lines · {s['mining_events']:,} mining · "
                 f"{s['compress_events']} compressions"]
        if s["unmatched_mining"]:
            parts.append(f"⚠ {s['unmatched_mining']} unrecognized mining "
                         f"lines - see debug.log")
        if not chars:
            parts.append("No mining activity seen yet - characters appear "
                         "automatically once their gamelogs show mining.")
        self.status.setText("\n".join(parts))
        self.status.setStyleSheet(
            "color: #f0b232;" if (not dir_ok or s["unmatched_mining"])
            else "color: #949ba4;")

        max_pct = max((c.pct for c in chars), default=0.0)
        gauge = make_tray_icon(max_pct)
        self.tray.setIcon(gauge)
        self.setWindowIcon(gauge)  # taskbar button shows the same live gauge
        if chars:
            self.setWindowTitle(f"{APP_NAME} - {max_pct:.0f}%")
        else:
            self.setWindowTitle(APP_NAME)
        def tip_line(c):
            eta = c.eta_full_s()
            return (f"{c.pct:.1f}%  {c.name}" +
                    (f"  ({fmt_eta(eta)})" if eta else ""))
        tip = "\n".join(tip_line(c) for c in chars[:8]) or APP_NAME
        self.tray.setToolTip(tip)


def main():
    try:
        verbose = json.loads((config_dir() / "settings.json").read_text(
            encoding="utf-8")).get("debug_verbose", False)
    except Exception:
        verbose = False
    setup_logging(verbose)
    log.info("=== Ore Hold Watcher starting (user=%s) ===", os.environ.get(
        "USERNAME") or os.environ.get("USER") or "?")
    log.info("config dir: %s", config_dir())
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(DARK_QSS)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
