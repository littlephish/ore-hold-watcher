"""Ore unit volumes (m3 per unit) for EVE Online.

Lookup rules used by engine.unit_volume():
  1. Exact name match (case-insensitive).
  2. "Compressed <X>" / "Batch Compressed <X>" -> COMPRESSED table, falling
     back to base volume / 100 if the base ore is known.
  3. Suffix match: "Concentrated Veldspar" ends with "Veldspar" -> base volume.
     This covers all +5% / +10% / +15% variants without listing them.
  4. User overrides from %APPDATA%/OreHoldWatcher/ores_override.json win over
     everything (exact-name entries, m3 per unit).

If nothing matches, the engine reports an "unknown ore" warning so the user
can add it to ores_override.json.
"""

# Base asteroid ores (uncompressed, m3/unit)
ORE_VOLUMES = {
    # High-sec / common
    "Veldspar": 0.1,
    "Scordite": 0.15,
    "Pyroxeres": 0.3,
    "Plagioclase": 0.35,
    "Omber": 0.6,
    "Kernite": 1.2,
    "Jaspet": 2.0,
    "Hemorphite": 3.0,
    "Hedbergite": 3.0,
    # Null / low
    "Gneiss": 5.0,
    "Dark Ochre": 8.0,
    "Ochre": 8.0,
    "Spodumain": 16.0,
    "Crokite": 16.0,
    "Bistot": 16.0,
    "Arkonor": 16.0,
    "Mercoxit": 40.0,
    # Trig / pochven
    "Bezdnacine": 16.0,
    "Rakovene": 16.0,
    "Talassonite": 16.0,
    # A0 blue star ores (Equinox)
    "Mordunium": 0.1,
    "Ytirium": 0.6,
    "Eifyrium": 2.0,
    "Ducinium": 16.0,
    "Griemeer": 0.4,
    "Hezorime": 2.0,
    "Kylixium": 0.6,
    "Nocxite": 1.0,
    "Ueganite": 8.0,
    # Ice (all ices are 1000 m3/unit)
    "Blue Ice": 1000.0,
    "Clear Icicle": 1000.0,
    "Glacial Mass": 1000.0,
    "White Glaze": 1000.0,
    "Glare Crust": 1000.0,
    "Dark Glitter": 1000.0,
    "Gelidus": 1000.0,
    "Krystallos": 1000.0,
    # Moon ores (all 10 m3/unit)
    "Zeolites": 10.0,
    "Sylvite": 10.0,
    "Bitumens": 10.0,
    "Coesite": 10.0,
    "Cobaltite": 10.0,
    "Euxenite": 10.0,
    "Titanite": 10.0,
    "Scheelite": 10.0,
    "Otavite": 10.0,
    "Sperrylite": 10.0,
    "Vanadinite": 10.0,
    "Chromite": 10.0,
    "Carnotite": 10.0,
    "Zircon": 10.0,
    "Pollucite": 10.0,
    "Cinnabar": 10.0,
    "Xenotime": 10.0,
    "Monazite": 10.0,
    "Loparite": 10.0,
    "Ytterbite": 10.0,
    # Gas (harvested units)
    "Fullerite-C28": 2.0,
    "Fullerite-C32": 5.0,
    "Fullerite-C50": 1.0,
    "Fullerite-C60": 1.0,
    "Fullerite-C70": 1.0,
    "Fullerite-C72": 2.0,
    "Fullerite-C84": 2.0,
    "Fullerite-C320": 5.0,
    "Fullerite-C540": 10.0,
    "Mykoserocin": 10.0,
    "Cytoserocin": 10.0,
}

# Compressed variants that differ from the base/100 rule can be listed here
# explicitly (m3 per unit). Anything not listed falls back to base/100.
COMPRESSED_VOLUMES = {
    # Compressed ice is 100 m3/unit (1000 / 10, not / 100)
    "Blue Ice": 100.0,
    "Clear Icicle": 100.0,
    "Glacial Mass": 100.0,
    "White Glaze": 100.0,
    "Glare Crust": 100.0,
    "Dark Glitter": 100.0,
    "Gelidus": 100.0,
    "Krystallos": 100.0,
}
