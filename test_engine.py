# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LittlePhish
"""Headless tests for the log engine. Run: python test_engine.py"""
import tempfile
import time
from pathlib import Path

from engine import (CharacterState, Engine, MiningEvent, HoldFullEvent,
                    UnknownOreEvent, OreTable, ResidueEvent, TargetRock,
                    fastest_rock, tick_bursts, ts_to_epoch)

HEADER = (
    "------------------------------------------------------------\n"
    "  Gamelog\n"
    "  Listener: {name}\n"
    "  Session Started: 2026.07.15 12:00:00\n"
    "------------------------------------------------------------\n"
)

LINES_A = HEADER.format(name="Neik Kondur") + "\n".join([
    "[ 2026.07.15 12:01:00 ] (mining) You have successfully mined 1,244 units of Veldspar.",
    "[ 2026.07.15 12:02:00 ] (mining) You have successfully mined 2,000 units of Concentrated Veldspar",
    "[ 2026.07.15 12:03:00 ] (mining) Your mining laser extracted 10 units of Blue Ice",
    "[ 2026.07.15 12:04:00 ] (mining) You mined 500 units of Compressed Veldspar",
    "[ 2026.07.15 12:05:00 ] (mining) 300 units of Spodumain was mined and transferred to your ore hold",
    "[ 2026.07.15 12:06:00 ] (mining) You have successfully mined 100 units of Bright Spodumain.",
    "[ 2026.07.15 12:07:00 ] (mining) 2,000 units of Veldspar were lost as residue",  # excluded
    "[ 2026.07.15 12:08:00 ] (combat) 312 from Guristas Rat - Wrecks",                 # ignored
    "[ 2026.07.15 12:09:00 ] (mining) You have successfully mined 42 units of Unobtanium!",  # unknown
    "[ 2026.07.15 12:10:00 ] (notify) Your ore hold is full.",
]) + "\n"

# Exact format observed in real gamelogs (drone mining + in-ship compression)
LINES_C = HEADER.format(name="Diese Nusse") + "\n".join([
    "[ 2026.07.15 13:11:29 ] (mining) <color=0x77ffffff>You mined <font size=12><color=#ff8dc169>11<color=0x77ffffff><font size=10> units of <color=0xffffffff><font size=12>Glistening Zeolites",
    "[ 2026.07.15 13:11:30 ] (mining) <color=0x77ffffff>You mined <font size=12><color=#ff8dc169>12<color=0x77ffffff><font size=10> units of <color=0xffffffff><font size=12>Glistening Sylvite",
    "[ 2026.07.15 14:13:06 ] (notify) Successfully compressed Glistening Zeolites into 10 Compressed Glistening Zeolites.",
    # service-provider line must NOT affect this character's hold
    "[ 2026.07.15 14:13:07 ] (notify) Edgar Hendar compressed 9014 Glistening Zeolites using your compression services.",
]) + "\n"

LINES_B = HEADER.format(name="Nancy Kondur") + "\n".join([
    "[ 2026.07.15 12:01:30 ] (mining) You have successfully mined 5,000 units of <color=0xff00ff00>Golden Omber</color>",
]) + "\n"

# Residue: depletes the asteroid but never enters the hold (spec F1).
# The residue line carries no ore name and must pair with the tick above it.
LINES_D = HEADER.format(name="Yuri Urt") + "\n".join([
    "[ 2026.07.15 15:00:00 ] (mining) You mined 13 units of Brimful Coesite",
    "[ 2026.07.15 15:00:00 ] (mining) Additional 13 units depleted from asteroid as residue",
    "[ 2026.07.15 15:01:00 ] (mining) You mined 14 units of Brimful Coesite",
    # >2s after any tick: unpaired, must be discarded
    "[ 2026.07.15 15:05:30 ] (mining) Additional 99 units depleted from asteroid as residue",
]) + "\n"


def approx(a, b, tol=0.01):
    assert abs(a - b) < tol, f"{a} != {b}"


def main():
    tmp = Path(tempfile.mkdtemp())
    # utf-8 with BOM (typical for gamelogs)
    (tmp / "20260715_120000_91234567.txt").write_bytes(
        b"\xef\xbb\xbf" + LINES_A.encode("utf-8"))
    # utf-16-le with BOM (be tolerant of either encoding)
    (tmp / "20260715_120100_95555555.txt").write_bytes(
        b"\xff\xfe" + LINES_B.encode("utf-16-le"))
    # real-world format: markup-laden drone mining + compression, CRLF
    (tmp / "20260715_130645_2123973494.txt").write_bytes(
        b"\xef\xbb\xbf" + LINES_C.replace("\n", "\r\n").encode("utf-8"))
    # residue lines (spec F1/F2)
    (tmp / "20260715_150000_93333333.txt").write_bytes(
        b"\xef\xbb\xbf" + LINES_D.encode("utf-8"))

    eng = Engine(log_dir=tmp, state_path=tmp / "state.json",
                 default_capacity=180000.0)
    events = eng.poll()

    mines = [e for e in events if isinstance(e, MiningEvent)]
    fulls = [e for e in events if isinstance(e, HoldFullEvent)]
    unknowns = [e for e in events if isinstance(e, UnknownOreEvent)]

    for e in events:
        print(e)

    assert len(fulls) == 1 and fulls[0].character == "Neik Kondur"
    assert len(unknowns) == 1 and unknowns[0].ore.rstrip("!") == "Unobtanium"

    by_ore = {e.ore: e for e in mines}
    approx(by_ore["Veldspar"].m3, 1244 * 0.1)
    approx(by_ore["Concentrated Veldspar"].m3, 2000 * 0.1)      # variant suffix
    approx(by_ore["Blue Ice"].m3, 10 * 1000)
    approx(by_ore["Compressed Veldspar"].m3, 500 * 0.001)       # base/100
    approx(by_ore["Spodumain"].m3, 300 * 16)
    approx(by_ore["Bright Spodumain"].m3, 100 * 16)
    approx(by_ore["Golden Omber"].m3, 5000 * 0.6)               # tag-stripped, utf-16

    nancy = eng.char("Nancy Kondur")
    approx(nancy.est_m3, 3000.0)

    # real-format file: 11 Zeolites-variant + 12 Sylvite-variant mined (10 m3
    # each), then 10 units compressed. Default mode assumes compressed ore is
    # dragged out of the hold -> full raw volume freed. The "using your
    # compression services" provider line must be ignored.
    diese = eng.char("Diese Nusse")
    approx(diese.est_m3, (11 + 12) * 10.0 - 10 * 10.0)

    # compressed-stays-in-hold mode: only the volume difference is freed
    eng_keep = Engine(log_dir=tmp, state_path=tmp / "state_keep.json",
                      compressed_leaves_hold=False)
    eng_keep.poll()
    approx(eng_keep.char("Diese Nusse").est_m3,
           (11 + 12) * 10.0 + 10 * (0.1 - 10.0))

    # --- residue (spec F1/F2) ---
    residues = [e for e in events if isinstance(e, ResidueEvent)]
    assert len(residues) == 1, f"expected 1 paired residue, got {len(residues)}"
    assert residues[0].qty == 13
    assert residues[0].ore == "Brimful Coesite", residues[0].ore
    assert residues[0].character == "Yuri Urt"

    # REGRESSION (spec F1): residue must never enter the ore hold.
    yuri = eng.char("Yuri Urt")
    approx(yuri.est_m3, (13 + 14) * 10.0)

    neik = eng.char("Neik Kondur")
    # hold-full event snaps to capacity
    approx(neik.est_m3, 180000.0)

    # --- incremental append -------------------------------------------------
    with open(tmp / "20260715_120100_95555555.txt", "ab") as f:
        f.write("[ 2026.07.15 12:20:00 ] (mining) You have successfully mined 1,000 units of Kernite\n"
                .encode("utf-16-le"))
    events2 = eng.poll()
    mines2 = [e for e in events2 if isinstance(e, MiningEvent)]
    assert len(mines2) == 1 and mines2[0].ore == "Kernite"
    approx(eng.char("Nancy Kondur").est_m3, 3000.0 + 1200.0)

    # --- reset / calibrate ----------------------------------------------------
    eng.reset("Neik Kondur")
    assert eng.char("Neik Kondur").est_m3 == 0.0
    eng.calibrate("Nancy Kondur", 50000)
    approx(eng.char("Nancy Kondur").est_m3, 50000.0)

    # --- state persists --------------------------------------------------------
    eng2 = Engine(log_dir=tmp, state_path=tmp / "state.json")
    approx(eng2.char("Nancy Kondur").est_m3, 50000.0)

    # --- scanned rock countdown (spec D1/D4) ---
    eng.set_target("Yuri Urt", ore="Brimful Coesite", units=1000,
                   distance_m=726.0)
    tr = eng.char("Yuri Urt").target
    assert tr is not None and tr.scan_units == 1000

    # depletion counts mined AND residue (spec F1)
    eng._apply_depletion(MiningEvent(character="Yuri Urt", qty=100,
                                     ore="Brimful Coesite", m3=1000.0,
                                     ts="2126.07.15 16:00:00"))
    eng._apply_depletion(ResidueEvent(character="Yuri Urt", qty=25,
                                      ore="Brimful Coesite",
                                      ts="2126.07.15 16:00:00"))
    assert eng.char("Yuri Urt").rock_remaining() == 875, \
        eng.char("Yuri Urt").rock_remaining()

    # a different ore does not deplete this rock
    eng._apply_depletion(MiningEvent(character="Yuri Urt", qty=500,
                                     ore="Bitumens", m3=5000.0,
                                     ts="2126.07.15 16:00:30"))
    assert eng.char("Yuri Urt").rock_remaining() == 875

    # warm-up: too few ticks to trust a rate (spec: "showing nothing beats
    # showing a confident wrong number"), and the UI must be able to say so
    assert eng.char("Yuri Urt").rock_eta_s() is None
    assert eng.char("Yuri Urt").rock_status() == "warmup", \
        eng.char("Yuri Urt").rock_status()

    # ticks before the scan anchor are ignored
    eng._apply_depletion(MiningEvent(character="Yuri Urt", qty=999,
                                     ore="Brimful Coesite", m3=9990.0,
                                     ts="2000.01.01 00:00:00"))
    assert eng.char("Yuri Urt").rock_remaining() == 875

    # DroneStopEvent zeroes an observed pop regardless of arithmetic (D4)
    eng.rock_popped("Yuri Urt")
    assert eng.char("Yuri Urt").target is None

    # over-depletion clears rather than going negative
    eng.set_target("Yuri Urt", ore="Coesite", units=10, distance_m=5.0)
    eng._apply_depletion(MiningEvent(character="Yuri Urt", qty=50,
                                     ore="Coesite", m3=500.0,
                                     ts="2126.07.15 17:00:00"))
    assert eng.char("Yuri Urt").target is None

    # a warmed-up rate produces a real ETA
    eng.set_target("Yuri Urt", ore="Coesite", units=1000, distance_m=5.0)
    for i in range(5):
        eng._apply_depletion(MiningEvent(
            character="Yuri Urt", qty=20, ore="Coesite", m3=200.0,
            ts=f"2126.07.15 18:{i:02d}:00"))
    yc = eng.char("Yuri Urt")
    assert yc.rock_remaining() == 900
    # 5 bursts one minute apart, 20 units each. The rate is what one CYCLE
    # removes: 20 units/min -> 900 units = 45 min. Counting all 5 bursts'
    # units across the 4-minute span they straddle would say 25 units/min,
    # a 25% overshoot, because the first burst was removed before the window
    # opened.
    eta = yc.rock_eta_s(now_epoch=ts_to_epoch("2126.07.15 18:04:00"))
    assert eta is not None, "warmed-up rate should yield an ETA"
    approx(eta, 900 / 20.0 * 60.0, tol=1.0)
    assert yc.rock_status(
        now_epoch=ts_to_epoch("2126.07.15 18:04:00")) == "ready"
    # long after the last tick the countdown pauses rather than lying
    assert yc.rock_status(
        now_epoch=ts_to_epoch("2126.07.15 23:00:00")) == "idle"

    # --- a system change abandons the tracked rock ---
    # Scanner distances are ship-relative and rocks are grid-local, so a rock
    # tracked in one system must not keep depleting from ticks mined in
    # another. A gate jump always changes system, so it always invalidates.
    sysdir = Path(tempfile.mkdtemp())
    (sysdir / "20260715_190000_94444444.txt").write_bytes(
        b"\xef\xbb\xbf" + (HEADER.format(name="Yuri Urt") + "\n".join([
            "[ 2026.07.15 19:00:00 ] (mining) You mined 100 units of Coesite",
            "[ 2026.07.15 19:05:00 ] (None) Jumping from AK-LNZ to NV-ZHM",
            "[ 2026.07.15 19:10:00 ] (mining) You mined 100 units of Coesite",
        ]) + "\n").encode("utf-8"))
    esys = Engine(log_dir=sysdir, state_path=sysdir / "s.json")
    esys.poll()
    esys.set_target("Yuri Urt", ore="Coesite", units=5000, distance_m=726.0)
    # ticks before the jump deplete the rock
    esys._apply_depletion(MiningEvent(character="Yuri Urt", qty=100,
                                      ore="Coesite", m3=1000.0,
                                      ts="2126.07.15 19:00:00"))
    assert esys.char("Yuri Urt").rock_remaining() == 4900
    # the jump drops the target; later ticks must not touch it
    esys.left_system("Yuri Urt")
    assert esys.char("Yuri Urt").target is None, \
        "a system change must abandon the tracked rock"

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

    # --- rate measurement: cycles, not log lines -------------------------------
    # A three-laser volley plus its residue line is ONE cycle. Counting the
    # lines would claim four times the evidence and, with a 2 s span, an
    # absurd rate.
    volley = [(0.0, 13), (2.0, 13), (3.0, 13), (3.0, 13),
              (60.0, 13), (61.0, 13), (62.0, 14)]
    b = tick_bursts(volley)
    assert len(b) == 2, b
    assert b[0] == (0.0, 52) and b[1] == (60.0, 40), b

    # a rock whose ore is already being mined has a rate the moment it is
    # armed - the ship's history is the ship's history, scan or no scan
    warm = Engine(log_dir=tmp, state_path=tmp / "state_warm.json")
    wc = warm.char("Yuri Urt")
    T = 2_000_000.0
    for i in range(4):          # 4 bursts, 40 s apart, 20 units each
        wc.note_tick(T + i * 40, "Zeolites", 20)
    warm.set_target("Yuri Urt", ore="Zeolites", units=1200, distance_m=253.0)
    NOWW = T + 120
    assert wc.rock_status(NOWW) == "ready", "history must survive set_target"
    # 3 bursts of work (the first only opens the window) over 120 s = 30/min
    approx(wc.rock_depletion_rate(NOWW), 30.0, tol=0.1)
    approx(wc.rock_eta_s(NOWW), 1200 / 30.0 * 60.0, tol=1.0)

    # ...and a span too short to be a real cycle still refuses to guess
    quick = CharacterState(name="Q")
    quick.target = TargetRock(ore="Zeolites", scan_units=1000,
                              scan_ts="2026.08.21 00:00:00", distance_m=10.0)
    quick.note_tick(T, "Zeolites", 13)
    quick.note_tick(T + 2, "Zeolites", 13)      # same volley
    quick.note_tick(T + 20, "Zeolites", 13)     # next cycle, span 20 s < 30
    assert quick.rock_status(T + 20) == "warmup"
    assert quick.rock_eta_s(T + 20) is None

    # --- mining a different ore is reported, not silently frozen --------------
    # The exact shape of a mis-picked rock: pilot is chewing Zeolites, the
    # tracked rock is Bitumens, so nothing can ever count down.
    mis = CharacterState(name="Yuri Urt")
    mis.target = TargetRock(ore="Bitumens", scan_units=5196,
                            scan_ts="2026.08.21 14:29:37", distance_m=253.0)
    for i in range(4):
        mis.note_tick(T + i * 40, "Zeolites", 13)
    assert mis.mining_ore(T + 120) == "Zeolites"
    assert mis.rock_status(T + 120) == "mismatch"
    assert mis.rock_eta_s(T + 120) is None
    assert mis.rock_active(T + 120) is False,         "ticks for another ore must never count as being on this rock"
    # once they stop entirely it is plain idleness, not a mix-up
    assert mis.rock_status(T + 5000) == "idle"

    # --- restored rocks are provisional: kept only while still being mined ---
    # The reload above proves the rock SURVIVES a restart. These prove it only
    # survives when the replayed log shows the pilot still on it.
    sp2 = tmp / "state_stale.json"
    e4 = Engine(log_dir=tmp, state_path=sp2, default_capacity=180000.0)
    e4.set_target("Yuri Urt", ore="Coesite", units=5000, distance_m=726.0)
    yu = e4.char("Yuri Urt")

    # nothing replayed for this rock yet -> not provably being mined. This is
    # the exact state a freshly reloaded target is in.
    assert yu.rock_status() == "idle"
    assert yu.rock_active() is False
    assert e4.drop_stale_targets() == ["Yuri Urt"]
    assert yu.target is None, "a rock with no live ticks must not be restored"

    # a rock with a tick inside RATE_IDLE_S survives the sweep
    e4.set_target("Yuri Urt", ore="Coesite", units=5000, distance_m=726.0)
    T0 = ts_to_epoch("2126.07.15 20:00:00")
    e4._apply_depletion(MiningEvent(character="Yuri Urt", qty=100,
                                    ore="Coesite", m3=1000.0,
                                    ts="2126.07.15 20:00:00"))
    assert e4.char("Yuri Urt").rock_active(T0 + 60) is True
    assert e4.drop_stale_targets(T0 + 60) == []
    assert e4.char("Yuri Urt").target is not None
    # ...and is dropped once those ticks age past it (RATE_IDLE_S = 5 min)
    assert e4.char("Yuri Urt").rock_active(T0 + 301) is False
    assert e4.drop_stale_targets(T0 + 301) == ["Yuri Urt"]
    assert e4.char("Yuri Urt").target is None

    # ticks for a DIFFERENT ore never count as "still on this rock": the log
    # names the ore on every tick, so this is checked, not assumed
    e4.set_target("Yuri Urt", ore="Coesite", units=5000, distance_m=726.0)
    e4._apply_depletion(MiningEvent(character="Yuri Urt", qty=100,
                                    ore="Bitumens", m3=1000.0,
                                    ts="2126.07.15 21:00:00"))
    assert e4.char("Yuri Urt").rock_active(
        ts_to_epoch("2126.07.15 21:00:30")) is False

    # a closed client drops the rock, and says so; a second call is a no-op
    e4.set_target("Yuri Urt", ore="Coesite", units=5000, distance_m=726.0)
    assert e4.client_closed("Yuri Urt") is True
    assert e4.char("Yuri Urt").target is None
    assert e4.client_closed("Yuri Urt") is False
    assert e4.client_closed("Nobody At All") is False

    # a state file with no target key loads cleanly (backward compatible)
    legacy = tmp / "state_legacy.json"
    legacy.write_text('{"characters": {"Solo Pilot": {"capacity": 180000.0}}}',
                      encoding="utf-8")
    e3 = Engine(log_dir=tmp, state_path=legacy, default_capacity=180000.0)
    assert e3.char("Solo Pilot").target is None

    # --- ore table edge cases ---------------------------------------------------
    t = OreTable()
    approx(t.unit_volume("Compressed Bright Spodumain"), 0.16)
    approx(t.unit_volume("Compressed Thick Blue Ice"), 100.0)
    approx(t.unit_volume("Magma Mercoxit"), 40.0)
    assert t.unit_volume("Tritanium") is None

    # --- fastest_rock: which countdown drives the gauge's inner ring -----------
    def rock_char(name, ore, units, left, rate_per_min=None, now=1_000_000.0):
        """A CharacterState with a scanned rock and, optionally, a live rate.

        rate_per_min=None leaves rock_events empty -> warm-up -> no ETA.
        """
        c = CharacterState(name=name)
        c.target = TargetRock(ore=ore, scan_units=units,
                              scan_ts="2026.07.15 12:00:00",
                              distance_m=1000.0,
                              depleted_units=units - left)
        if rate_per_min:
            # 4 bursts 40 s apart clears ROCK_WARMUP_BURSTS/ROCK_WARMUP_S.
            # The first burst is the window's opening edge and contributes no
            # units to the rate, so 3 bursts of work span 120 s.
            per_burst = rate_per_min * 2.0 / 3
            for i in range(4):
                c.note_tick(now - 120 + i * 40, ore, per_burst)
        return c

    NOW = 1_000_000.0
    assert fastest_rock([]) is None
    # a dry rock is nothing to count down
    assert fastest_rock([rock_char("A", "Coesite", 5000, 0, 100)], NOW) is None

    # soonest ETA wins even though it has proportionally MORE rock left
    a = rock_char("A", "Coesite", 10000, 8000, 4000)    # 80% left, 2 min
    b = rock_char("B", "Bitumens", 10000, 2000, 200)    # 20% left, 10 min
    r = fastest_rock([b, a], NOW)
    assert r.character == "A" and r.ore == "Coesite" and r.remaining == 8000
    approx(r.fraction, 0.8)
    approx(r.eta_s, 120.0, tol=1.0)

    # no ETA yet (warm-up/idle) sorts last, but still draws a ring...
    c = rock_char("C", "Veldspar", 10000, 9000)
    assert fastest_rock([c, b], NOW).character == "B"
    solo = fastest_rock([c], NOW)
    assert solo.character == "C" and solo.eta_s is None
    approx(solo.fraction, 0.9)
    # ...and among ETA-less rocks the emptiest one wins
    d = rock_char("D", "Scordite", 10000, 3000)
    assert fastest_rock([c, d], NOW).character == "D"

    # residue overshoot clamps at empty instead of a negative ring
    over = rock_char("E", "Omber", 10000, -500)
    assert fastest_rock([over], NOW) is None

    # a character with no rock at all is simply skipped
    assert fastest_rock([CharacterState(name="F"), d], NOW).character == "D"

    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
