# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LittlePhish
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version. It is distributed WITHOUT ANY WARRANTY; see the GNU
# General Public License (the LICENSE file, or <https://www.gnu.org/licenses/>)
# for details.
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
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog,
                               QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QMainWindow, QMenu, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton,
                               QScrollArea, QSpinBox, QSystemTrayIcon,
                               QVBoxLayout, QWidget, QCheckBox)

from engine import (Engine, MiningEvent, HoldFullEvent, UnknownOreEvent,
                    CombatEvent, DroneStopEvent, ts_to_epoch)
from scan import parse_scan

APP_NAME = "Ore Hold Watcher"
ORG_DIR = "OreHoldWatcher"
# Stable Windows identity for the taskbar button and toast notifications, so
# both behave the same however the app is launched (see set_app_user_model_id).
APP_USER_MODEL_ID = "LittlePhish.OreHoldWatcher"
DEFAULT_UPDATE_REPO = "littlephish/ore-hold-watcher"
# Fallback version for source runs. The built exe carries the real version
# stamped from the git tag by release.yml; that wins when available.
APP_VERSION = "1.0.0"

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
# every file kept in the config dir - used for one-time migration when the
# config location moves between the portable (beside-exe) and AppData layouts
_CONFIG_FILES = ("settings.json", "state.json", "ores_override.json",
                 "ledger.json", "prices.json")


def _migrate_config(src: Path, dst: Path) -> None:
    """Copy any config files present in `src` into `dst`, never overwriting."""
    if not src.is_dir() or src.resolve() == dst.resolve():
        return
    import shutil
    for f in _CONFIG_FILES:
        s = src / f
        if s.exists() and not (dst / f).exists():
            try:
                shutil.copy2(s, dst / f)
            except OSError:
                pass


def config_dir() -> Path:
    """Where settings / state / ledger live.

    Installed and packaged builds keep config in %APPDATA%\\OreHoldWatcher,
    OUTSIDE the program folder, so the folder-swap updater and the uninstaller
    never touch user data. Source runs stay portable (beside the source tree)
    for convenient dev. A one-time migration copies any config found in the
    other location, so nothing is lost when the layout changes."""
    global _CONFIG_DIR
    if _CONFIG_DIR is not None:
        return _CONFIG_DIR
    appdata = _appdata_dir()
    beside = app_base_dir()
    if is_frozen() or is_packaged():
        appdata.mkdir(parents=True, exist_ok=True)
        d = appdata
        _migrate_config(beside, d)      # honor a portable config placed by hand
    elif _writable(beside):
        d = beside
        _migrate_config(appdata, d)     # pick up any prior AppData config once
    else:
        appdata.mkdir(parents=True, exist_ok=True)
        d = appdata
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
    "privacy_mode": False,   # hide folder path + anonymize names (for sharing)
    "compressed_leaves_hold": True,  # you drag compressed ore to fleet hangar
    "alert_interval_min": 5.0,  # at most one alert per X minutes (0 = every alert)
    "idle_alert_enabled": True,  # alert when a pilot stops receiving ore ticks
    "idle_alert_min": 5.0,       # ... for this many minutes
    "allclear_enabled": False,   # green "resolved" note when an issue clears
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
    "update_repo": "littlephish/ore-hold-watcher",  # GitHub owner/repo
    "update_skip_version": "",  # a version the user chose to skip permanently
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
        self.last_focused: str | None = None   # most recent foreground pilot

    def refresh(self):
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes
            user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
            found: set[str] = set()
            count = [0]
            fg = user32.GetForegroundWindow()

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
                                who = title.split(" - ", 1)[1].strip()
                                found.add(who)
                                # remember which client you were last looking
                                # at, so a scan pasted after alt-tabbing still
                                # attributes to the right pilot (spec D3)
                                if hwnd == fg:
                                    self.last_focused = who
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


def is_packaged() -> bool:
    """True when running inside an MSIX/AppX package. The package install dir
    is read-only and tamper-protected, so the in-place exe self-updater must
    stay off - MSIX updates come from App Installer / a reinstall instead."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        length = ctypes.c_uint32(0)
        # GetCurrentPackageFullName: APPMODEL_ERROR_NO_PACKAGE (15700) when
        # unpackaged; ERROR_INSUFFICIENT_BUFFER (122) when packaged.
        rc = ctypes.windll.kernel32.GetCurrentPackageFullName(
            ctypes.byref(length), None)
        return rc != 15700
    except Exception:
        return False


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


def app_version_str() -> str:
    """Displayable version: the exe's stamped version when built, else the
    source fallback marked as such."""
    v = current_exe_version()
    return f"v{v}" if v else f"v{APP_VERSION} (source)"


_EXE_NAME = "OreHoldWatcher.exe"


def install_dir() -> Path:
    """Folder holding the running executable - the thing an update replaces."""
    return Path(sys.executable).resolve().parent


def can_write_install_dir() -> bool:
    """False when the program folder needs elevation we never request (e.g. a
    machine-wide Program Files install). Then an in-place update is impossible
    and we point the user at a reinstall instead of failing silently."""
    try:
        probe = install_dir() / ".upd_write_test"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


# Written to a NO-SPACES temp folder and run by cmd.exe to finish an update
# after we exit. Deliberately cmd + robocopy, NOT PowerShell: a machine
# ExecutionPolicy of AllSigned/Restricted (locked-down or corporate machines)
# overrides -ExecutionPolicy Bypass and silently refuses to run an unsigned
# .ps1 - the swap simply never happens and no log is written. Batch has no such
# gate. robocopy /MIR mirrors the new program folder over the install dir and
# removes files an older version left behind; the excludes protect the Inno
# uninstaller, this log, and any stale staging folder. The install path (which
# contains spaces - "Ore Hold Watcher") is passed as a quoted arg and read via
# %~2, so spaces are safe. Args: %1=src folder  %2=install dir  %3=exe name.
# It ALWAYS relaunches the exe at the end, even if robocopy reported problems,
# so the app never fails to come back up.
_SWAP_BAT = r"""@echo off
setlocal enableextensions
cd /d "%TEMP%"
set "SRC=%~1"
set "DST=%~2"
set "EXE=%~3"
set "LOG=%DST%\update-log.txt"
echo [%date% %time%] updater started: "%SRC%" to "%DST%" > "%LOG%"
rem give the app a moment to exit so its files unlock (robocopy also retries)
ping -n 3 127.0.0.1 >nul
rem mirror the new build over the install; /R:60 /W:1 retries files still held
rem by a slow exit; excludes protect the uninstaller, this log and old staging
robocopy "%SRC%" "%DST%" /MIR /XD "%DST%\update" /XF unins000.exe unins000.dat update-log.txt /R:60 /W:1 >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] robocopy exit %RC% >> "%LOG%"
if %RC% GEQ 8 echo [%date% %time%] WARNING: robocopy reported errors (see above) >> "%LOG%"
echo [%date% %time%] starting "%DST%\%EXE%" >> "%LOG%"
start "" "%DST%\%EXE%"
echo [%date% %time%] done >> "%LOG%"
"""


class Updater:
    """Checks GitHub releases for a newer -win64.zip, downloads it, and swaps
    the whole program folder in place via a detached PowerShell helper. Network
    work runs in daemon threads; the UI polls the fields.

    Folder swap, not exe swap: the shipped artifact is a Nuitka --standalone
    program folder (onefile tripped AV dropper heuristics), so an update is
    'download the new folder, mirror it over the old one, relaunch'."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.busy = False
        self.available: dict | None = None   # {"version", "url", "current"}
        self.up_to_date: str | None = None   # latest tag when already current
        self.error: str | None = None
        self.downloaded: str | None = None   # path of the fetched update .zip
        self.manual = False

    def repo(self) -> str:
        # hardcoded default so updates work out of the box; a non-empty
        # setting can still override it
        return (str(self.s["update_repo"]).strip().strip("/")
                or DEFAULT_UPDATE_REPO)

    def can_update(self) -> bool:
        return (bool(self.repo()) and is_frozen() and sys.platform == "win32"
                and not is_packaged() and can_write_install_dir())

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
            # the portable program-folder zip; keep "win" in the asset name
            asset = next((a for a in data.get("assets", [])
                          if a.get("name", "").lower().endswith(".zip")
                          and "win" in a.get("name", "").lower()), None)
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
        import tempfile
        try:
            base = Path(tempfile.mkdtemp(prefix="orewatcher-update-"))
            dest = base / "update.zip"
            req = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
            with urllib.request.urlopen(req, timeout=300) as r, \
                    open(dest, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            if dest.stat().st_size < 1_000_000:  # sanity: the folder zip is big
                raise ValueError("downloaded file suspiciously small")
            self.downloaded = str(dest)
            log.info("update downloaded: %s (%d bytes)", dest,
                     dest.stat().st_size)
        except Exception as e:
            log.warning("update download failed: %s", e)
            self.error = str(e)
        finally:
            self.busy = False

    # -- phase 3: swap + restart ---------------------------------------------
    def _extract(self, zip_path: Path) -> Path:
        """Unpack the zip and return the folder that holds the executable
        (the zip usually nests the program folder one level deep)."""
        import zipfile
        out = zip_path.parent / "unpacked"
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(out)
        if (out / _EXE_NAME).exists():
            return out
        for child in out.rglob(_EXE_NAME):
            return child.parent
        raise RuntimeError(f"{_EXE_NAME} not found in the downloaded archive")

    @staticmethod
    def _spawn_detached(args: list[str]) -> bool:
        """Launch a helper fully detached so it survives our exit."""
        import subprocess
        DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0x8)
        NEWGROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
        BREAKAWAY = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x1000000)
        for flags in (DETACHED | NEWGROUP | BREAKAWAY, DETACHED | NEWGROUP):
            try:
                subprocess.Popen(args, creationflags=flags, close_fds=True)
                log.info("update helper launched: %s (flags=0x%x)",
                         args[0], flags)
                return True
            except OSError as e:
                log.warning("update launch failed (flags=0x%x): %s", flags, e)
        return False

    def apply(self) -> bool:
        """Unpack the new build and launch the detached swap helper, returning
        True so the caller can quit. The helper waits for our exe to unlock,
        mirrors the new folder over the install dir, and relaunches.

        Preferred helper is the Rust update.exe, run from a temp COPY so it can
        overwrite the whole install (including update.exe itself) with no
        interpreter and no self-replace problem. We take it from the NEWLY
        DOWNLOADED build first, so a fix to the updater ships and takes effect
        on the very same update; the currently-installed update.exe is the next
        choice, and a cmd/robocopy batch the last resort. Either way the helper
        runs from the no-spaces temp dir and the install path is passed as a
        quoted arg, so 'Ore Hold Watcher' spaces are safe."""
        if not self.downloaded:
            return False
        import shutil
        try:
            new_dir = self._extract(Path(self.downloaded))
        except Exception as e:
            log.warning("update extract failed: %s", e)
            self.error = str(e)
            return False
        target = install_dir()
        tmp = Path(self.downloaded).parent

        # prefer the update.exe shipped INSIDE the new build (self-fixing), then
        # the installed one; run from a temp copy so it can replace its own
        # installed copy during the swap
        for source in (new_dir / "update.exe", target / "update.exe"):
            if not source.exists():
                continue
            try:
                tmp_exe = tmp / "update.exe"
                shutil.copy2(source, tmp_exe)
                if self._spawn_detached(
                        [str(tmp_exe), str(new_dir), str(target), _EXE_NAME]):
                    return True
            except OSError as e:
                log.warning("update.exe helper (%s) failed, trying next: %s",
                            source, e)

        # last resort: cmd/robocopy (no PowerShell -> immune to execution policy)
        bat = tmp / "apply_update.bat"
        try:
            bat.write_text(_SWAP_BAT, encoding="ascii")
        except OSError as e:
            log.warning("could not write fallback update script: %s", e)
            self.error = str(e)
            return False
        return self._spawn_detached(
            ["cmd", "/c", str(bat), str(new_dir), str(target), _EXE_NAME])


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


def fmt_dur(seconds: float) -> str:
    """Human duration for time-in-state cells: '2h 05m', '37m', '48s'."""
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def fill_color(pct: float) -> str:
    if pct >= 90:
        return "#f23f43"   # red
    if pct >= 75:
        return "#f0b232"   # amber
    return "#23a55a"       # green


def make_gauge_pixmap(pct: float) -> QPixmap:
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
    return pm


def make_tray_icon(pct: float) -> QIcon:
    return QIcon(make_gauge_pixmap(pct))


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
                t = Notification(app_id=APP_USER_MODEL_ID, title=title, msg=body)
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
        if (payload or {}).get("allclear"):   # green regardless of fill
            color = 0x23A55A
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
        self.bump = QLabel('<a href="#" style="color:#f0b232;">⤴ resize</a>')
        self.bump.setToolTip("This hold ran past its configured capacity - "
                             "click to bump it to a larger ship size")
        self.bump.setVisible(False)
        self.bump.linkActivated.connect(
            lambda *_: self.main.suggest_bump(self.name))
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
        top.addWidget(self.bump)
        top.addWidget(self.amount)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(4)
        lay.addLayout(top)
        lay.addWidget(self.bar)
        # Second line, only present when a scanned rock is being tracked
        # (spec D5) - the window grows only when the feature is in use.
        self.rock = QLabel("")
        self.rock.setObjectName("rockLine")
        self.rock.setVisible(False)
        lay.addWidget(self.rock)

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
        # nudge: show "resize" only while the hold is over capacity BUT still
        # within the ship class's max-skill size - past that it's estimate
        # drift, not a too-small capacity, so stop nudging
        smax = ship_max_hold(cap)
        self.bump.setVisible(cap * 1.005 < est <= smax + 1)
        self.bar.setValue(int(min(pct, 100) * 10))
        self.bar.setStyleSheet(
            f"QProgressBar::chunk {{ background: {col}; border-radius: 4px; }}")

    def menu(self, pos):
        m = QMenu(self)
        m.addAction("Reset (hold emptied)", lambda: self.main.reset_char(self.name))
        m.addAction("Set current m³…", lambda: self.main.calibrate_char(self.name))
        m.addAction("Set capacity…", lambda: self.main.capacity_char(self.name))
        m.addSeparator()
        m.addAction("Paste survey scan…",
                    lambda: ScanPasteDialog(self.main, self.name).exec())
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
# Time-in-state buckets (engine key, display label, color). Colors match the
# app's fill palette: mining green, idle amber, full red, offline grey.
ACTIVITY_STATES = [
    ("mining",  "Mining",  "#23a55a"),
    ("idle",    "Idle",    "#f0b232"),
    ("full",    "Full",    "#f23f43"),
    ("offline", "Offline", "#6d6f78"),
]
CHART_SURFACE = "#313338"
CHART_GRID = "#3f4147"
CHART_TEXT = "#dbdee1"
CHART_MUTED = "#949ba4"


def bold_font(base) -> "QFont":
    """A bold copy of a base font with a guaranteed-valid size. The app font
    is pixel-sized (QSS 'font-size: 13px'), so pointSize() is -1 on it;
    copying that and letting Qt re-derive a point size triggers a harmless
    'setPointSize <= 0' warning. Rebuilding with the pixel size avoids it."""
    f = QFont(base)
    px, pt = base.pixelSize(), base.pointSize()
    if px > 0:
        f.setPixelSize(px)
    elif pt > 0:
        f.setPointSize(pt)
    else:
        f.setPixelSize(13)
    f.setBold(True)
    return f


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
        self.color_map: dict = {}          # optional fixed series -> color
        self.setMouseTracking(True)
        self.setMinimumHeight(260)
        self._hit: list[tuple] = []        # (QRect, char, day, value)

    def set_data(self, days, series, values, unit, labels=None, color_map=None):
        self.days, self.series, self.values, self.unit = days, series, values, unit
        self.labels = labels or [d[5:] for d in days]  # default MM.DD
        self.color_map = color_map or {}
        self.update()

    def color_for(self, char: str) -> str:
        if char in self.color_map:
            return self.color_map[char]
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
        if main.settings["privacy_mode"]:   # stable aliases across all history
            main.seed_aliases(c for day in main.engine.ledger["days"].values()
                              for c in day)
            main.seed_aliases(
                c for day in main.engine.ledger.get("activity", {}).values()
                for c in day)
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

        # Activity tab: per-day time-in-state, plus a stacked-hours trend
        act_tab = QWidget()
        aclay = QVBoxLayout(act_tab)
        adrow = QHBoxLayout()
        adrow.addWidget(QLabel("Day (EVE/UTC):"))
        self.act_day_combo = QComboBox()
        for d in sorted(main.engine.ledger.get("activity", {}), reverse=True):
            self.act_day_combo.addItem(d)
        self.act_day_combo.currentTextChanged.connect(
            lambda *_: self.populate_activity())
        adrow.addWidget(self.act_day_combo, 1)
        aclay.addLayout(adrow)

        self.act_tree = QTreeWidget()
        self.act_tree.setColumnCount(6)
        self.act_tree.setHeaderLabels(
            ["Character"] + [lbl for _, lbl, _ in ACTIVITY_STATES] + ["Total"])
        self.act_tree.setRootIsDecorated(False)
        aclay.addWidget(self.act_tree, 1)

        acrow = QHBoxLayout()
        acrow.addWidget(QLabel("Trend range:"))
        self.act_range_combo = QComboBox()
        for lbl, days in (("7 days", 7), ("30 days", 30), ("90 days", 90),
                          ("1 year", 365), ("All", 0)):
            self.act_range_combo.addItem(lbl, days)
        self.act_range_combo.setCurrentIndex(1)   # 30 days
        self.act_range_combo.currentTextChanged.connect(
            lambda *_: self.update_activity_chart())
        acrow.addWidget(self.act_range_combo)
        acrow.addStretch(1)
        aclay.addLayout(acrow)
        self.act_chart = LedgerChart()
        aclay.addWidget(self.act_chart, 1)
        act_note = QLabel(
            "Time each pilot spent Mining / Idle / Full / Offline, measured "
            "while the watcher was running (not reconstructed from old logs). "
            "Offline needs client detection on; otherwise a logged-off pilot "
            "counts as Idle.")
        act_note.setWordWrap(True)
        act_note.setStyleSheet("color: #949ba4;")
        aclay.addWidget(act_note)

        from PySide6.QtWidgets import QTabWidget
        tabs = QTabWidget()
        tabs.addTab(day_tab, "Day detail")
        tabs.addTab(trend_tab, "Trend")
        tabs.addTab(act_tab, "Activity")

        lay = QVBoxLayout(self)
        lay.addWidget(tabs, 1)
        lay.addWidget(self.status)
        lay.addWidget(bb)
        # taller by default: the Activity tab stacks a table AND a chart, so
        # 520 clipped the chart. This fits both without scrolling.
        self.resize(680, 720)

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
        self.populate_activity()
        self.update_activity_chart()

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
        # display names (aliased in privacy mode); "Other" stays "Other"
        dn = self.main.disp
        series = [dn(c) for c in top] + (
            ["Other"] if len(char_totals) > len(top) else [])
        values = {}
        for bkey in order:
            per = raw[bkey]
            dd = {dn(ch): per.get(ch, 0.0) for ch in top}
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
                [self.main.disp(char), f"{c_units:,}", f"{c_m3:,.0f}",
                 fmt_isk(c_isk if not partial or c_isk else None)
                 + (" (partial)" if partial and c_isk else "")])
            f = bold_font(parent.font(0))
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
        f = bold_font(total.font(0))
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

    def populate_activity(self):
        from PySide6.QtWidgets import QTreeWidgetItem
        self.act_tree.clear()
        day = self.act_day_combo.currentText()
        data = self.main.engine.ledger.get("activity", {}).get(day, {})
        keys = [k for k, _, _ in ACTIVITY_STATES]
        totals = {k: 0.0 for k in keys}
        for char in sorted(data, key=lambda c: c.lower()):
            st = data[char]
            row_total = 0.0
            cells = [self.main.disp(char)]
            for k in keys:
                v = float(st.get(k, 0.0))
                row_total += v
                totals[k] += v
                cells.append(fmt_dur(v) if v else "-")
            cells.append(fmt_dur(row_total))
            item = QTreeWidgetItem(cells)
            item.setFont(0, bold_font(item.font(0)))
            self.act_tree.addTopLevelItem(item)
        grand = sum(totals.values())
        tot = QTreeWidgetItem(
            ["TOTAL"] + [fmt_dur(totals[k]) if totals[k] else "-" for k in keys]
            + [fmt_dur(grand) if grand else "-"])
        f = bold_font(tot.font(0))
        for col in range(6):
            tot.setFont(col, f)
        self.act_tree.addTopLevelItem(tot)
        for col in range(6):
            self.act_tree.resizeColumnToContents(col)

    def update_activity_chart(self):
        act = self.main.engine.ledger.get("activity", {})
        days_all = sorted(act)
        span_days = int(self.act_range_combo.currentData())
        if span_days and days_all:
            from datetime import date, timedelta
            y, m, d = (int(x) for x in days_all[-1].split("."))
            cutoff = (date(y, m, d) - timedelta(days=span_days - 1)).strftime(
                "%Y.%m.%d")
            day_keys = [d for d in days_all if d >= cutoff]
        else:
            day_keys = days_all

        n = len(day_keys)
        grp = "day" if n <= 31 else ("week" if n <= 183 else "month")
        series = [lbl for _, lbl, _ in ACTIVITY_STATES]
        color_map = {lbl: col for _, lbl, col in ACTIVITY_STATES}

        values: dict = {}
        labels: dict = {}
        order: list = []
        for day in day_keys:
            bkey, blab = self._bucket(day, grp)
            if bkey not in values:
                values[bkey] = {lbl: 0.0 for lbl in series}
                labels[bkey] = blab
                order.append(bkey)
            per = values[bkey]
            for st in act[day].values():
                for key, lbl, _ in ACTIVITY_STATES:
                    per[lbl] += float(st.get(key, 0.0)) / 3600.0  # -> hours
        self.act_chart.set_data(order, series, values, "h",
                                labels=[labels[k] for k in order],
                                color_map=color_map)


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


def ship_max_hold(capacity: float) -> float:
    """The max-skill hold size of the ship class a configured capacity most
    likely belongs to: the smallest max-skill (level-V) value >= capacity.
    Used to bound the 'resize' nudge - a hold over its own ship's max is
    estimate drift, not a too-small capacity, so we stop nudging there."""
    at_vs = sorted(v for _, _, v, _ in SHIP_ORE_HOLDS)
    return next((a for a in at_vs if a >= capacity - 1), at_vs[-1])


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
        self.pilot.currentIndexChanged.connect(lambda *_: self.preselect_rock())

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
            tick = self.main.engine._last_tick.get(who)
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

        self.allclear = QCheckBox("Also send an all-clear (green) note when an "
                                  "issue resolves (mining resumes, hold back "
                                  "to safe)")
        self.allclear.setChecked(bool(settings["allclear_enabled"]))
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
        self.privacy = QCheckBox("Privacy mode for screenshots (hide folder "
                                 "path, alias names to Pilot 1/2/…)")
        self.privacy.setChecked(bool(settings["privacy_mode"]))
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
        self.upd_check = QCheckBox("Check GitHub for app updates (daily, from "
                                   + DEFAULT_UPDATE_REPO + ")")
        self.upd_check.setChecked(bool(settings["update_check"]))

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
        gf.addRow(self.privacy)
        gf.addRow(self.comp_out)
        gf.addRow(self.cwatch)
        gf.addRow(self.ledger_on)
        gf.addRow(self.ledger_prices)
        gf.addRow(self.ledger_backfill)
        gf.addRow(self.upd_check)
        gf.addRow(self.dbg)
        gf.addRow(self.open_log)
        tabs.addTab(gen, "General")

        al = QWidget()
        af = QFormLayout(al)
        af.addRow("Min. time between alerts:", self.interval)
        af.addRow(self.idle_on)
        af.addRow("Idle after:", self.idle_min)
        af.addRow(self.allclear)
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

        notice = QLabel(
            'Ore Hold Watcher © 2026 LittlePhish - free software under the GNU '
            'GPL v3 or later; comes with ABSOLUTELY NO WARRANTY. '
            '<a href="https://www.gnu.org/licenses/gpl-3.0.html" '
            'style="color:#6d6f78;">License</a> · '
            '<a href="https://github.com/littlephish/ore-hold-watcher" '
            'style="color:#6d6f78;">Source</a><br>'
            'EVE Online and the EVE logo are trademarks of CCP hf. This app '
            'is not affiliated with or endorsed by CCP. '
            '© CCP hf. All rights reserved.')
        notice.setOpenExternalLinks(True)
        notice.setWordWrap(True)
        notice.setStyleSheet("color: #6d6f78; font-size: 10px;")

        root = QVBoxLayout(self)
        root.addWidget(tabs)
        root.addWidget(bb)
        root.addWidget(notice)
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
        self.s["allclear_enabled"] = self.allclear.isChecked()
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
        self.s["privacy_mode"] = self.privacy.isChecked()
        self.s["compressed_leaves_hold"] = self.comp_out.isChecked()
        self.s["client_watch_enabled"] = self.cwatch.isChecked()
        self.s["ledger_enabled"] = self.ledger_on.isChecked()
        self.s["ledger_fetch_prices"] = self.ledger_prices.isChecked()
        self.s["ledger_backfill_prices"] = self.ledger_backfill.isChecked()
        self.s["update_check"] = self.upd_check.isChecked()
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
        # character -> scan_ts already warned about; re-arms on re-anchor
        self._rock_warned: dict[str, str] = {}
        self._last_client_scan = 0.0
        self._last_price_check = 0.0
        self._last_activity_ts = 0.0    # wall-clock of last time-in-state accrual
        self._last_activity_save = 0.0  # throttle ledger writes for activity

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
        ver = QLabel(app_version_str())
        ver.setStyleSheet("color: #6d6f78; font-size: 11px;")
        ver.setToolTip("Running version. Updates come from "
                       + DEFAULT_UPDATE_REPO)
        hdr.addWidget(ver)
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
        # refresh throttling / repaint caches: the poll tick runs every
        # poll_seconds for alerting, but the (much heavier) visual refresh is
        # rate-limited and only repaints tray/rows when values actually change
        self._last_refresh = 0.0
        self._tray_icon_key = None    # last pct the gauge was painted for
        self._gauge_pixmap = None     # cached gauge; repainted only on pct change
        self._tray_tip = ""
        self._win_title = ""
        self._applied_order: list | None = None  # row order last laid out

        # Tray
        self.tray = QSystemTrayIcon(make_tray_icon(0), self)
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        menu.addAction("Show / Hide", self.toggle_visible)
        menu.addAction("Recalculate from logs", self.recalculate)
        menu.addAction("Paste survey scan…",
                       lambda: ScanPasteDialog(self).exec())
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
        # pilots with an open problem, awaiting an all-clear:
        self._pending_resume: set[str] = set()  # idle/drone alerted; clears on mining
        self._pending_safe: set[str] = set()    # threshold/full; clears below re-arm
        self._alert_pending = False
        self._pending_title = ""

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(int(float(self.settings["poll_seconds"]) * 1000))
        self.tick()

    # -- privacy / display helpers -------------------------------------------
    _aliases: dict = {}

    def seed_aliases(self, names) -> None:
        """Assign an alias to any not-yet-seen character. Append-only so an
        existing alias never changes mid-render (which caused collisions);
        seeding the full roster in sorted order gives Pilot 1..N by name."""
        for n in sorted(set(names), key=str.lower):
            if n not in self._aliases:
                self._aliases[n] = f"Pilot {len(self._aliases) + 1}"

    def disp(self, name: str) -> str:
        """Character name for display. In privacy mode, a stable alias
        (Pilot 1, Pilot 2, …); alerts and webhooks still use real names."""
        if not self.settings["privacy_mode"]:
            return name
        if name not in self._aliases:
            self.seed_aliases([name])
        return self._aliases[name]

    def disp_path(self, path) -> str:
        """Watched-folder path for display; masked in privacy mode."""
        s = str(path)
        if not self.settings["privacy_mode"]:
            return s
        for marker in ("\\EVE\\", "/EVE/"):
            i = s.find(marker)
            if i >= 0:
                return "…" + s[i:]
        return "…\\Gamelogs"

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
        # rows/status are skipped while hidden, so resync them now the window
        # is visible again
        self._applied_order = None       # force a re-layout on next refresh
        QTimer.singleShot(0, self.refresh)

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
        if self.settings["ledger_enabled"]:
            self.engine.save_ledger()   # persist the last unsaved activity slice
        self.tray.hide()
        QApplication.quit()

    # -- notifications --------------------------------------------------------
    def notify(self, title: str, body: str, payload: dict | None = None):
        self.notifier.alert(title, body, payload)

    def send_allclear(self, title: str):
        """A green 'resolved' notification through every enabled method."""
        body, payload = self.fleet_summary()
        payload["allclear"] = True
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

    def _rock_alert(self, c):
        """Soft, local-only warning that a rock is about to run dry (D6).

        Strip miners get no popped-rock line in the log at all (spec F3), so
        without this a minimised window means no warning. Deliberately NOT
        routed through Notifier.alert(), which fans out to popup, sound,
        webhook and ntfy - this alert never leaves the machine.
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
        if self.settings["notify_overlay"]:
            self.notifier.overlay.show_alert(
                f"{self.disp(c.name)}: {c.target.ore} rock nearly dry")

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

    def suggest_bump(self, name: str):
        """The hold ran past its configured capacity. Offer to bump it to the
        next standard ship hold size at or above the current estimate, or to
        the estimate itself if it's already bigger than any standard size."""
        c = self.engine.char(name)
        # the max-skill size of this hold's ship class (what the nudge is
        # bounded to); offer that as the bump target
        target = ship_max_hold(c.capacity)
        est_round = int(round(c.est_m3 / 500.0) * 500)   # tidy number
        options = []
        if target > c.capacity + 1:
            ship = next((n for n, b, a, _ in SHIP_ORE_HOLDS if a == target), "")
            options.append((f"{target:,.0f} m³  ({ship} max skill)",
                            float(target)))
        options.append((f"{est_round:,} m³  (current estimate)",
                        float(est_round)))
        options.append(("Choose a value…", None))

        from PySide6.QtWidgets import QInputDialog
        labels = [o[0] for o in options]
        choice, ok = QInputDialog.getItem(
            self, "Bump capacity",
            f"{self.disp(name)}'s hold passed its {c.capacity:,.0f} m³ "
            f"capacity (now ~{c.est_m3:,.0f} m³).\n"
            "Set its capacity to:", labels, 0, False)
        if not ok:
            return
        val = dict((o[0], o[1]) for o in options)[choice]
        if val is None:
            val, ok2 = QInputDialog.getDouble(
                self, "Set capacity", f"Ore hold capacity for "
                f"{self.disp(name)} (m³):", c.capacity, 1, 10_000_000, 0)
            if not ok2:
                return
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
        if is_packaged():
            QMessageBox.information(
                self, "Updates", "This is the MSIX build - updates are managed "
                "by Windows (App Installer / reinstall the package), so the "
                "in-app updater is disabled.")
            return
        if not is_frozen():
            QMessageBox.information(
                self, "Updates", "Running from source - update by pulling "
                "the repo. Auto-update only applies to the built exe.")
            return
        if not can_write_install_dir():
            QMessageBox.information(
                self, "Updates", "This install lives in a folder that needs "
                "administrator rights to change, so the in-app updater can't "
                "apply updates here. Download the latest release and reinstall "
                "to update.")
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
            ver = info["version"]
            skipped = str(self.settings["update_skip_version"])
            # a manual "Check for updates" always shows the dialog, even for
            # a version previously skipped or already prompted this session
            if u.manual or (ver not in self._update_prompted and ver != skipped):
                self._update_prompted.add(ver)
                box = QMessageBox(self)
                box.setWindowTitle("Update available")
                box.setText(f"Version {ver} is available "
                            f"(you have {info['current']}).")
                box.setInformativeText(
                    "Update now downloads it and restarts the app. Later asks "
                    "again next launch. Skip this version won't ask again "
                    "for it.")
                b_update = box.addButton("Update now", QMessageBox.AcceptRole)
                box.addButton("Later", QMessageBox.RejectRole)
                b_skip = box.addButton("Skip this version",
                                       QMessageBox.DestructiveRole)
                style_titlebar(box)
                box.exec()
                clicked = box.clickedButton()
                if clicked is b_update:
                    self.status.setText("")  # (main window status untouched)
                    u.download_async(info["url"])
                    self.tray.showMessage(
                        APP_NAME, f"Downloading {ver}… the app will restart "
                        "when it's ready.", QSystemTrayIcon.Information, 5000)
                elif clicked is b_skip:
                    self.settings["update_skip_version"] = ver
                    self.settings.save()
        if u.downloaded:
            # consented earlier; warn at the actual restart moment too
            self.tray.showMessage(APP_NAME, "Update downloaded - restarting "
                                  "now.", QSystemTrayIcon.Information, 3000)
            if u.apply():          # swap script waits for our exit
                self.quit()
            else:
                u.downloaded = None
        # daily re-check
        if (self.settings["update_check"] and u.can_update() and
                time.time() - self._last_update_check > 86_400):
            self._last_update_check = time.time()
            u.check_async()

    # -- time-in-state accounting -----------------------------------------------
    def _accrue_activity(self, is_closed) -> None:
        """Add the elapsed wall-clock since the last tick to each tracked
        pilot's current-state bucket for today (UTC). State priority:
        offline (client closed) > full > mining > idle. Only counts normal
        tick gaps - a long gap (sleep/stall) is skipped rather than guessed,
        and the whole thing is off unless the ledger is enabled."""
        if not self.settings["ledger_enabled"]:
            self._last_activity_ts = 0.0     # so resume doesn't dump a big gap
            return
        now = time.time()
        dt = now - self._last_activity_ts
        self._last_activity_ts = now
        poll_s = max(1.0, float(self.settings["poll_seconds"]))
        if not (0 < dt <= poll_s * 3 + 1):   # first tick / abnormal gap
            return
        day = time.strftime("%Y.%m.%d", time.gmtime())
        recorded = False
        for c in self.engine.chars.values():
            if is_closed(c.name):
                state = "offline"
            elif c.est_m3 >= c.capacity * 0.999:
                state = "full"
            elif c.mining_rate_m3_min(now) > 0:
                state = "mining"
            else:
                state = "idle"
            if self.engine.activity_add(c.name, state, dt, day):
                recorded = True
        if recorded and now - self._last_activity_save > 60:
            self._last_activity_save = now
            self.engine.save_ledger()

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
                # all-clear: this pilot was flagged idle/drone-stopped and is
                # now mining again
                if (self.settings["allclear_enabled"] and
                        ev.character in self._pending_resume):
                    self._pending_resume.discard(ev.character)
                    self.send_allclear(
                        f"✅ {self.disp(ev.character)} - mining resumed")
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
                    self._pending_resume.add(ev.character)
                    self.request_alert(
                        f"🛑 {ev.character} - mining drone(s) stopped "
                        f"(asteroid depleted)")
            if isinstance(ev, HoldFullEvent):
                c = self.engine.char(ev.character)
                if not c.notified:
                    c.notified = True
                    self._pending_safe.add(ev.character)
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
                self._pending_safe.add(c.name)
                self.request_alert(f"⚠ {c.name} - {c.pct:.1f}% full")
            elif c.pct < rearm and c.notified:
                c.notified = False
                # all-clear: hold dropped back to a safe level (unloaded,
                # compressed, or reset)
                if (self.settings["allclear_enabled"] and
                        c.name in self._pending_safe):
                    self._pending_safe.discard(c.name)
                    self.send_allclear(
                        f"✅ {self.disp(c.name)} - hold back to safe "
                        f"({c.pct:.1f}%)")
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
                    self._pending_resume.add(c.name)
                    self.request_alert(
                        f"⏸ {c.name} - no ore ticks for {int(gap // 60)} min")
        self._accrue_activity(is_closed)
        self._flush_alert()          # send any alert the rate limiter held back
        self._check_downtime_close()
        self._maintain_prices()
        self._pump_updates()
        # Decouple the visual refresh from the poll tick: repaint promptly when
        # something actually happened (mining events), otherwise only every few
        # seconds - and rarely while hidden in the tray, where nothing is shown.
        min_gap = 2.0 if self.isVisible() else 10.0
        if events or now_utc - self._last_refresh >= min_gap:
            self._last_refresh = now_utc
            self.refresh()

    def refresh(self):
        chars = sorted(self.engine.chars.values(),
                       key=lambda c: c.pct, reverse=True)
        if self.settings["privacy_mode"]:   # stable Pilot 1..N by real name
            self.seed_aliases(c.name for c in chars)

        # --- tray gauge / tooltip / title: this path runs even while hidden,
        # so only touch the shell when the displayed value actually changed.
        # Repainting the icon and calling setIcon (a Win32 shell notify) every
        # tick was the bulk of the idle CPU. ---
        max_pct = max((c.pct for c in chars), default=0.0)
        icon_key = round(min(max_pct, 100.0), 1)
        if icon_key != self._tray_icon_key:
            self._tray_icon_key = icon_key
            self._gauge_pixmap = make_gauge_pixmap(max_pct)  # repaint on change
            self.tray.setIcon(QIcon(self._gauge_pixmap))     # tray: shell call
        # Windows 11's taskbar button ignores a repeated identical QIcon and
        # drops the icon when the native window is recreated (always-on-top
        # toggle). Push a FRESH QIcon from the cached pixmap every visible
        # refresh so the taskbar actually repaints - cheap (no re-paint), and
        # only while the window is shown.
        if self._gauge_pixmap is not None and self.isVisible():
            self.setWindowIcon(QIcon(self._gauge_pixmap))
        title = f"{APP_NAME} - {max_pct:.0f}%" if chars else APP_NAME
        if title != self._win_title:
            self._win_title = title
            self.setWindowTitle(title)

        def tip_line(c):
            eta = c.eta_full_s()
            return (f"{c.pct:.1f}%  {self.disp(c.name)}" +
                    (f"  ({fmt_eta(eta)})" if eta else ""))
        tip = "\n".join(tip_line(c) for c in chars[:8]) or APP_NAME
        if tip != self._tray_tip:
            self._tray_tip = tip
            self.tray.setToolTip(tip)

        # --- everything below is visual detail that's pointless while the
        # window is hidden in the tray; skip it entirely. showEvent forces a
        # full refresh when the window reappears. ---
        if not self.isVisible():
            return

        wanted = [c.name for c in chars]
        # drop rows for removed chars
        for name in list(self.rows):
            if name not in wanted:
                frame, _ = self.rows.pop(name)
                frame.setParent(None)
                frame.deleteLater()
                self._applied_order = None   # layout changed -> force reorder
        # (re)build rows, only re-laying them out when the order actually changed
        reorder = wanted != self._applied_order
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
                reorder = True
            frame, row = self.rows[c.name]
            if reorder:
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
            row.lbl.setText(self.disp(c.name))
            row.update_state(c.est_m3, c.capacity, c.eta_full_s(), arm)
            row.update_rock(c.target, c.rock_remaining(), c.rock_eta_s(),
                            self.disp(c.name))
            self._rock_alert(c)
        if reorder:
            self._applied_order = wanted

        # who fills up first at current mining rates?
        etas = [(c.eta_full_s(), c) for c in chars]
        etas = [(e, c) for e, c in etas if e is not None and e > 0]
        first_full = min(etas, key=lambda t: t[0]) if etas else None

        s = self.engine.stats
        dir_ok = self.engine.log_dir.is_dir()
        parts = []
        if first_full:
            parts.append(f"⏳ First hold full: {self.disp(first_full[1].name)} "
                         f"in ~{fmt_eta(first_full[0])}")
        parts += [f"{'Watching' if dir_ok else '⚠ MISSING FOLDER'}: "
                 f"{self.disp_path(self.engine.log_dir)}",
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


def set_app_user_model_id() -> None:
    """Give the process a stable taskbar identity. Without this Windows guesses
    it from whoever launched us, so a build relaunched by the updater lands
    under a different identity than one started from the Start menu - and in
    that state Win11 shows the static exe icon on the taskbar instead of the
    live gauge. Must run before any window is created."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            ctypes.c_wchar_p(APP_USER_MODEL_ID))
    except Exception as e:
        log.debug("set AppUserModelID failed: %s", e)


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
    set_app_user_model_id()   # stable taskbar identity, before any window
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(DARK_QSS)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
