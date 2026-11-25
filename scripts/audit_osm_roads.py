"""Audit the drawn geometry against the raw OpenStreetMap road network.

Every edge in `osm_delhi.graphml` came from a real OSM way, so there are no
artificial bridge hops to hide behind. This measures each point along each
drawn line against the nearest OSM road segment, and separately checks that
every drawn link is an actual graph edge rather than a straight jump between
two stations.

A chord across a block shows up as tens of metres. A point on a road shows up
at a few metres (Float32 storage rounding only).
"""
from __future__ import annotations

import math
from pathlib import Path

import duckdb
import networkx as nx
import numpy as np
from scipy.spatial import cKDTree

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "data" / "processed" / "reference.duckdb"
GRAPH = ROOT / "data" / "processed" / "osm_delhi.graphml"
OUT = ROOT / "web-next" / "public" / "data"

PIECE_M = 20.0
OFF_ROAD_M = 25.0
SAMPLE_M = 10.0


def main() -> int:
    print(f"loading {GRAPH}", flush=True)
    G = nx.read_graphml(GRAPH)
    print(f"graph: {len(G.nodes):,} nodes, {len(G.edges):,} edges", flush=True)

    node_ids = list(G.nodes)
    index = {n: i for i, n in enumerate(node_ids)}
    xs = np.array([G.nodes[n]["x"] for n in node_ids], dtype=np.float64)
    ys = np.array([G.nodes[n]["y"] for n in node_ids], dtype=np.float64)

    edges = np.array(
        [(index[u], index[v]) for u, v in G.edges()], dtype=np.int32
    )
    edge_set = {(int(u), int(v)) for u, v in edges}
    edge_set |= {(v, u) for u, v in edge_set}

    mean_lat = float(ys.mean())
    kx = 111320 * math.cos(math.radians(mean_lat))

    a = np.column_stack((xs[edges[:, 0]], ys[edges[:, 0]]))
    b = np.column_stack((xs[edges[:, 1]], ys[edges[:, 1]]))
    seg_m = np.hypot((b[:, 0] - a[:, 0]) * kx, (b[:, 1] - a[:, 1]) * 110540)
    steps = np.maximum(1, np.ceil(seg_m / PIECE_M)).astype(np.int32)
    total = int(steps.sum())

    p0 = np.empty((total, 2))
    p1 = np.empty((total, 2))
    k = 0
    for i in range(len(a)):
        n = int(steps[i])
        t0 = np.arange(n) / n
        t1 = (np.arange(n) + 1) / n
        p0[k:k + n] = a[i] + (b[i] - a[i]) * t0[:, None]
        p1[k:k + n] = a[i] + (b[i] - a[i]) * t1[:, None]
        k += n
    print(f"reference: {len(edges):,} OSM edges -> {total:,} pieces of <= {PIECE_M:.0f} m", flush=True)

    xy0 = np.column_stack((p0[:, 0] * kx, p0[:, 1] * 110540))
    xy1 = np.column_stack((p1[:, 0] * kx, p1[:, 1] * 110540))
    tree = cKDTree((xy0 + xy1) / 2)

    def dist_batch(points: np.ndarray) -> np.ndarray:
        xy = np.column_stack((points[:, 0] * kx, points[:, 1] * 110540))
        _, idx = tree.query(xy, k=8)
        best = np.full(len(xy), np.inf)
        for col in range(idx.shape[1]):
            j = idx[:, col]
            aa, bb = xy0[j], xy1[j]
            d = bb - aa
            den = np.einsum("ij,ij->i", d, d)
            t = np.clip(np.einsum("ij,ij->i", xy - aa, d) / np.maximum(den, 1e-12), 0, 1)
            proj = aa + d * t[:, None]
            off = np.sqrt(np.einsum("ij,ij->i", xy - proj, xy - proj))
            best = np.minimum(best, off)
        return best

    froms = np.fromfile(OUT / "geom_from.bin", dtype=np.int32)
    tos = np.fromfile(OUT / "geom_to.bin", dtype=np.int32)
    offs = np.fromfile(OUT / "geom_off.bin", dtype=np.int32)
    xy = np.fromfile(OUT / "geom_xy.bin", dtype=np.float32).reshape(-1, 2)

    n = len(froms)
    if len(offs) != n + 1:
        raise RuntimeError(f"offset array has {len(offs)} entries for {n} pairs")

    node_by_xy = {
        (float(np.float32(xs[i])), float(np.float32(ys[i]))): node_ids[i]
        for i in range(len(node_ids))
    }
    # Distinct OSM nodes can share identical coordinates (a dual-carriageway
    # junction, a duplicated vertex). A dict keyed by position then maps two
    # nodes to one key, and a link between them reads as a self-link that is
    # not an edge. Resolve a position to the *set* of nodes there instead.
    nodes_at: dict[tuple[float, float], set[str]] = {}
    for i in range(len(node_ids)):
        nodes_at.setdefault(
            (float(np.float32(xs[i])), float(np.float32(ys[i]))), set()
        ).add(node_ids[i])

    con = duckdb.connect(str(REF), read_only=True)
    station_pt = {
        int(sid): (float(lon), float(lat))
        for sid, lat, lon in con.execute(
            "select station_id, lat, lon from stations"
        ).fetchall()
    }

    def near(x: float, y: float, pt: tuple[float, float]) -> bool:
        return abs(x - pt[0]) * kx < 2.0 and abs(y - pt[1]) * 110540 < 2.0

    sampled: list[np.ndarray] = []
    missing = 0
    bad_pairs = 0
    worst: list[tuple[int, int, float]] = []
    link_kind = {"road": 0, "projection": 0, "zero-length": 0, "not-road": 0}
    chord_like = 0

    for i in range(n):
        pts = xy[offs[i]: offs[i + 1]]
        if len(pts) < 2:
            missing += 1
            continue
        sa = station_pt.get(int(froms[i]))
        sb = station_pt.get(int(tos[i]))
        seg_pts: list[np.ndarray] = []
        for j in range(len(pts) - 1):
            p, q = pts[j], pts[j + 1]
            if not (np.isfinite(p).all() and np.isfinite(q).all()):
                continue
            ax, ay = float(p[0]), float(p[1])
            bx, by = float(q[0]), float(q[1])
            if sa is not None and sb is not None:
                if (near(ax, ay, sa) and near(bx, by, sb)) or (
                    near(ax, ay, sb) and near(bx, by, sa)
                ):
                    chord_like += 1
            u = node_by_xy.get((float(np.float32(ax)), float(np.float32(ay))))
            v = node_by_xy.get((float(np.float32(bx)), float(np.float32(by))))
            if u is not None and v is not None and (index[u], index[v]) in edge_set:
                link_kind["road"] += 1
            elif (u is None) != (v is None):
                # One end is a station's projection point on a road segment and
                # the other is that segment's endpoint: a real road link.
                link_kind["projection"] += 1
            elif u is not None and v is not None:
                # Both ends resolve to real nodes. Distinct OSM nodes can share
                # identical coordinates, so a single-node lookup reports a
                # self-link that is not an edge. Accept the link if any node at
                # one end connects to any node at the other.
                us = nodes_at[(float(np.float32(ax)), float(np.float32(ay)))]
                vs = nodes_at[(float(np.float32(bx)), float(np.float32(by)))]
                if any((index[p], index[q]) in edge_set for p in us for q in vs):
                    link_kind["road"] += 1
                elif ax == bx and ay == by:
                    # Zero-length: both endpoints at one position, so nothing is
                    # drawn. Not a road link, but not a shortcut either.
                    link_kind["zero-length"] += 1
                else:
                    link_kind["not-road"] += 1
            else:
                link_kind["not-road"] += 1
            seg_m = math.hypot(
                (bx - ax) * 111320 * math.cos(math.radians((ay + by) / 2)),
                (by - ay) * 110540,
            )
            steps_n = max(1, math.ceil(seg_m / SAMPLE_M))
            t = np.arange(steps_n + 1) / steps_n
            seg_pts.append(np.column_stack((ax + (bx - ax) * t, ay + (by - ay) * t)))
        if not seg_pts:
            missing += 1
            continue
        arr = np.vstack(seg_pts)
        d = dist_batch(arr)
        sampled.append(d)
        mx = float(d.max())
        if mx > OFF_ROAD_M:
            bad_pairs += 1
            worst.append((int(froms[i]), int(tos[i]), mx))

    all_d = np.concatenate(sampled)
    print()
    print(f"pairs in geometry file: {n:,}")
    print(f"  with drawn geometry: {n - missing:,}")
    print(f"  with no geometry (omitted from the map): {missing:,}")
    print()
    print(f"rendered-line samples at {SAMPLE_M:.0f} m spacing: {len(all_d):,}")
    print(f"  median distance to an OSM road: {np.median(all_d):.2f} m")
    print(f"  p90: {np.percentile(all_d, 90):.2f} m")
    print(f"  p99: {np.percentile(all_d, 99):.2f} m")
    print(f"  max: {all_d.max():.2f} m")
    print(f"  share farther than {OFF_ROAD_M:.0f} m: {100 * (all_d > OFF_ROAD_M).mean():.3f}%")
    print(f"  pairs with any sample > {OFF_ROAD_M:.0f} m: {bad_pairs:,}")
    print()
    print(
        f"drawn links: on-an-OSM-edge={link_kind['road']:,} "
        f"station-projection={link_kind['projection']:,} "
        f"zero-length={link_kind['zero-length']:,} "
        f"not-an-edge={link_kind['not-road']:,}"
    )
    print(f"pairs drawn as a bare station-to-station chord: {chord_like}")

    if worst:
        print("\nworst pairs:")
        for a_id, b_id, d in sorted(worst, key=lambda r: r[2], reverse=True)[:15]:
            print(f"  {a_id}->{b_id}: {d:.1f} m")

    have = {(int(froms[i]), int(tos[i])) for i in range(n) if offs[i + 1] > offs[i]}
    print("\ndemo route coverage:")
    for route in ("241DOWN", "274AUP", "930DOWN"):
        row = con.execute(
            "select route_id from routes where route_name = ? limit 1", [route]
        ).fetchone()
        if not row:
            print(f"  {route}: not found")
            continue
        ids = [
            r[0]
            for r in con.execute(
                "select station_id from station_patterns where route_id = ? order by stop_sequence",
                [row[0]],
            ).fetchall()
        ]
        pairs = list(zip(ids, ids[1:]))
        miss = [p for p in pairs if p not in have]
        print(f"  {route}: {len(pairs)} pairs, missing={len(miss)}" + (f" {miss}" if miss else ""))

    failed = bad_pairs > 0 or link_kind["not-road"] > 0 or chord_like > 0
    print(f"\nAUDIT_EXIT={1 if failed else 0}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
