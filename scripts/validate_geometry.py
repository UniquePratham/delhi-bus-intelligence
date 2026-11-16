"""Validate reconstructed geometry against straight chords and real run times."""
from __future__ import annotations

import math
from pathlib import Path

import duckdb
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "web-next" / "public" / "data"
REF = ROOT / "data" / "processed" / "reference.duckdb"


def haversine(a, b):
    lat1, lon1 = a
    lat2, lon2 = b
    p = math.pi / 180
    dlat = (lat2 - lat1) * p
    dlon = (lon2 - lon1) * p
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin(dlon / 2) ** 2
    )
    return 2 * 6371000 * math.asin(math.sqrt(h))


froms = np.fromfile(OUT / "geom_from.bin", dtype=np.int32)
tos = np.fromfile(OUT / "geom_to.bin", dtype=np.int32)
offs = np.fromfile(OUT / "geom_off.bin", dtype=np.int32)
xy = np.fromfile(OUT / "geom_xy.bin", dtype=np.float32).reshape(-1, 2)

con = duckdb.connect(str(REF), read_only=True)
stations = {
    sid: (lat, lon)
    for sid, lat, lon in con.execute("SELECT station_id, lat, lon FROM stations").fetchall()
}

ratios = []
lengths = []
for i in range(len(froms)):
    a, b = int(froms[i]), int(tos[i])
    pts = xy[offs[i] : offs[i + 1]]
    if len(pts) < 2:
        continue
    path = sum(
        haversine((pts[j][1], pts[j][0]), (pts[j + 1][1], pts[j + 1][0]))
        for j in range(len(pts) - 1)
    )
    chord = haversine(stations[a], stations[b])
    if chord < 50:
        continue
    ratios.append(path / chord)
    lengths.append((chord, path))

r = np.array(ratios)
print(f"pairs measured: {len(r)}")
print(f"path/chord ratio  mean={r.mean():.3f}  median={np.median(r):.3f}")
print(f"  p10={np.percentile(r,10):.3f}  p90={np.percentile(r,90):.3f}  max={r.max():.2f}")
print(f"  share > 1.02 (curved): {(r > 1.02).mean()*100:.1f}%")
print(f"  share < 0.98 (shortcut): {(r < 0.98).mean()*100:.1f}%")

# Implied speed sanity: path length / observed median travel time.
runs = ROOT / "data" / "processed" / "gps" / "date=2026_10_15" / "station_runs.parquet"
med = {
    (f, t): s
    for f, t, s in con.execute(
        f"""
        SELECT from_station, to_station, median(travel_s)
        FROM read_parquet('{runs.as_posix()}')
        WHERE travel_s BETWEEN 20 AND 1200
        GROUP BY 1, 2
        """
    ).fetchall()
}

speeds = []
for i in range(len(froms)):
    key = (int(froms[i]), int(tos[i]))
    if key not in med:
        continue
    pts = xy[offs[i] : offs[i + 1]]
    if len(pts) < 2:
        continue
    path = sum(
        haversine((pts[j][1], pts[j][0]), (pts[j + 1][1], pts[j + 1][0]))
        for j in range(len(pts) - 1)
    )
    if path < 100:
        continue
    speeds.append(path / med[key] * 3.6)

s = np.array(speeds)
print()
print(f"implied speed km/h  median={np.median(s):.1f}  p10={np.percentile(s,10):.1f}  p90={np.percentile(s,90):.1f}")
print(f"  share in 5-60 km/h (plausible bus): {((s>=5)&(s<=60)).mean()*100:.1f}%")
