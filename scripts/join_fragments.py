"""Remove NaN gap sentinels and join the fragments they split.

The geometry files contain 6,441 NaN sentinels across 5,056 pairs, left over
from an earlier pass that broke lines at non-road connectors. The browser treats
each one as a break, which is why the route is drawn as dashes.

Those breaks are no longer wanted: the geometry is now snapped to roads, so a
fragment boundary is a few metres of GPS noise, not a jump across a block.

This removes every sentinel and joins the fragments, keeping the line continuous
unless the two sides are further apart than MAX_JOIN_M, in which case the pair
is left with a single fragment rather than a line across a gap.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

OUT = Path("web-next/public/data")
MAX_JOIN_M = 80.0


def metres(a, b) -> float:
    kx = 111320 * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((b[0] - a[0]) * kx, (b[1] - a[1]) * 110540)


def main() -> int:
    offs = np.fromfile(f"{OUT}/geom_off.bin", dtype=np.int32)
    xy = np.fromfile(f"{OUT}/geom_xy.bin", dtype=np.float32).reshape(-1, 2)

    out_off = np.zeros(len(offs), dtype=np.int32)
    out_xy: list[float] = []
    joined = 0
    dropped = 0

    for i in range(len(offs) - 1):
        seg = xy[offs[i] : offs[i + 1]]
        if len(seg) == 0:
            out_off[i + 1] = len(out_xy) // 2
            continue

        # Split into fragments at the sentinels.
        frags: list[list[tuple[float, float]]] = []
        cur: list[tuple[float, float]] = []
        for lon, lat in seg:
            if np.isfinite(lon) and np.isfinite(lat):
                cur.append((float(lon), float(lat)))
            else:
                if len(cur) >= 2:
                    frags.append(cur)
                cur = []
        if len(cur) >= 2:
            frags.append(cur)

        if not frags:
            out_off[i + 1] = len(out_xy) // 2
            continue

        # Join consecutive fragments when their ends are close.
        merged: list[tuple[float, float]] = list(frags[0])
        for f in frags[1:]:
            gap = metres(merged[-1], f[0])
            if gap <= MAX_JOIN_M:
                merged.extend(f)
                joined += 1
            else:
                # Too far apart to join: keep the longer fragment only, so the
                # pair draws one clean line instead of two disconnected ones.
                dropped += 1
                if len(f) > len(merged):
                    merged = list(f)

        for lon, lat in merged:
            out_xy.extend((lon, lat))
        out_off[i + 1] = len(out_xy) // 2

    (OUT / "geom_off.bin").write_bytes(out_off.tobytes())
    (OUT / "geom_xy.bin").write_bytes(np.asarray(out_xy, dtype=np.float32).tobytes())

    print(f"fragments joined: {joined}")
    print(f"fragments dropped (too far): {dropped}")
    size = __import__("os").path.getsize(OUT / "geom_xy.bin")
    print(f"points: {len(out_xy) // 2}  bytes: {size / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
