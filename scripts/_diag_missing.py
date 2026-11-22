"""Check whether the dashes come from pairs with no geometry."""
from __future__ import annotations

import duckdb
import numpy as np

REF = "data/processed/reference.duckdb"
OUT = "web-next/public/data"

froms = np.fromfile(f"{OUT}/geom_from.bin", dtype=np.int32)
tos = np.fromfile(f"{OUT}/geom_to.bin", dtype=np.int32)
offs = np.fromfile(f"{OUT}/geom_off.bin", dtype=np.int32)

have = {(int(froms[i]), int(tos[i])) for i in range(len(froms)) if offs[i + 1] > offs[i]}
print(f"pairs with geometry: {len(have)} / {len(froms)}")

con = duckdb.connect(REF, read_only=True)
names = dict(con.execute("select station_id, name from stations").fetchall())

for route in ("241DOWN", "274AUP", "930DOWN"):
    rid = con.execute(
        "select route_id from routes where route_name=? limit 1", [route]
    ).fetchone()[0]
    ids = [
        r[0]
        for r in con.execute(
            "select station_id from station_patterns where route_id=? order by stop_sequence",
            [rid],
        ).fetchall()
    ]
    missing = [(a, b) for a, b in zip(ids, ids[1:]) if (a, b) not in have]
    print(f"\n{route}: {len(ids)-1} pairs, {len(missing)} missing")
    for a, b in missing:
        print(f"   {a}->{b}  {names.get(a)} -> {names.get(b)}")
