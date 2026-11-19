"""Insert explicit geometry breaks wherever a routed link is not a raw road edge."""
from __future__ import annotations

import pickle

import numpy as np

from route_on_roads import CACHE, OUT


def main() -> int:
    g = pickle.loads(CACHE.read_bytes())
    if "road_edges" not in g:
        raise RuntimeError("Rebuild the road graph with raw-edge provenance first")
    coords = g["coords"]
    ids = {
        (float(np.float32(lon)), float(np.float32(lat))): i
        for i, (lon, lat) in enumerate(coords)
    }
    road_edges = set(g["road_edges"])
    froms = np.fromfile(OUT / "geom_from.bin", dtype=np.int32)
    tos = np.fromfile(OUT / "geom_to.bin", dtype=np.int32)
    offs = np.fromfile(OUT / "geom_off.bin", dtype=np.int32)
    xy = np.fromfile(OUT / "geom_xy.bin", dtype=np.float32).reshape(-1, 2)

    new_xy: list[tuple[float, float]] = []
    new_off = np.zeros(len(froms) + 1, dtype=np.int32)
    split_links = 0
    failed_lookup = 0
    for i in range(len(froms)):
        pts = xy[offs[i] : offs[i + 1]]
        if len(pts):
            new_xy.append((float(pts[0, 0]), float(pts[0, 1])))
            for j in range(1, len(pts)):
                p0 = (float(pts[j - 1, 0]), float(pts[j - 1, 1]))
                p1 = (float(pts[j, 0]), float(pts[j, 1]))
                u, v = ids.get(p0), ids.get(p1)
                if u is None or v is None:
                    failed_lookup += 1
                    new_xy.append((float("nan"), float("nan")))
                elif (min(u, v), max(u, v)) not in road_edges:
                    new_xy.append((float("nan"), float("nan")))
                    split_links += 1
                new_xy.append(p1)
        new_off[i + 1] = len(new_xy)

    (OUT / "geom_off.bin").write_bytes(new_off.tobytes())
    (OUT / "geom_xy.bin").write_bytes(np.asarray(new_xy, dtype=np.float32).reshape(-1, 2).tobytes())
    print(f"pairs: {len(froms)}  artificial links split: {split_links}  unknown links split: {failed_lookup}")
    print(f"points including gap sentinels: {len(new_xy)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
