"""Remove small closed loops from the drawn geometry.

A loop is a stretch where the path returns close to a point it already visited
after travelling a short distance. On the map it reads as a small rectangle or
hook hanging off the route.

For each pair, walk the points and drop any subpath that closes back on itself
within LOOP_CLOSE_M after travelling less than LOOP_MAX_M.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

OUT = Path("web-next/public/data")
LOOP_CLOSE_M = 30.0
LOOP_MAX_M = 400.0


def metres(a, b) -> float:
    kx = 111320 * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((b[0] - a[0]) * kx, (b[1] - a[1]) * 110540)


def main() -> int:
    offs = np.fromfile(OUT / "geom_off.bin", dtype=np.int32)
    xy = np.fromfile(OUT / "geom_xy.bin", dtype=np.float32).reshape(-1, 2)

    out_off = np.zeros(len(offs), dtype=np.int32)
    out_xy: list[float] = []
    removed = 0

    for i in range(len(offs) - 1):
        seg = xy[offs[i] : offs[i + 1]]
        pts = [
            (float(lon), float(lat))
            for lon, lat in seg
            if np.isfinite(lon) and np.isfinite(lat)
        ]
        if len(pts) < 3:
            for lon, lat in pts:
                out_xy.extend((lon, lat))
            out_off[i + 1] = len(out_xy) // 2
            continue

        kept: list[tuple[float, float]] = [pts[0]]
        for p in pts[1:]:
            # Look back for a point that p closes onto.
            cut = -1
            walked = 0.0
            for j in range(len(kept) - 1, -1, -1):
                if j < len(kept) - 1:
                    walked += metres(kept[j], kept[j + 1])
                if walked > LOOP_MAX_M:
                    break
                if metres(kept[j], p) <= LOOP_CLOSE_M and walked > LOOP_CLOSE_M:
                    cut = j
                    break
            if cut >= 0:
                # Drop the loop: keep up to the point it closed onto.
                removed += len(kept) - cut
                kept = kept[: cut + 1]
            kept.append(p)

        for lon, lat in kept:
            out_xy.extend((lon, lat))
        out_off[i + 1] = len(out_xy) // 2

    (OUT / "geom_off.bin").write_bytes(out_off.tobytes())
    (OUT / "geom_xy.bin").write_bytes(np.asarray(out_xy, dtype=np.float32).tobytes())

    print(f"loop points removed: {removed}")
    print(f"points: {len(out_xy) // 2}  bytes: {(OUT / 'geom_xy.bin').stat().st_size / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
