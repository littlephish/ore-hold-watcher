"""Headless tests for the log engine. Run: python test_engine.py"""
import tempfile
import time
from pathlib import Path

from engine import Engine, MiningEvent, HoldFullEvent, UnknownOreEvent, OreTable

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

    # --- ore table edge cases ---------------------------------------------------
    t = OreTable()
    approx(t.unit_volume("Compressed Bright Spodumain"), 0.16)
    approx(t.unit_volume("Compressed Thick Blue Ice"), 100.0)
    approx(t.unit_volume("Magma Mercoxit"), 40.0)
    assert t.unit_volume("Tritanium") is None

    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
