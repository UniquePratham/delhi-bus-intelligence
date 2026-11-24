"""Route every station pair on the real OSM road network.

The graph is the Delhi extract from OpenStreetMap: 922,941 nodes and 1,961,233
edges in one strongly connected component, with real junction topology and
one-way tags.

Anchoring
---------
A station is not an OSM node. Snapping it to the *nearest node* puts the line
end wherever a mapper happened to place a vertex, which can be a hundred metres
down the road, and it forces every pair through that one vertex. An earlier
version then searched alternative nearby nodes to dodge U-turns and joined the
winner back to the canonical node with a straight line. That join is not a road:
an audit found 6,916 drawn links that were not OSM edges and 899 pairs with a
point more than 25 m from any road.

Instead each station is projected onto the nearest road *segment*. The
projection point is on the road by construction, so it becomes a synthetic node
with two edges to that segment's endpoints, weighted by the split of the
segment's length. Every drawn point is then either an OSM node or a point on an
OSM segment, and consecutive pairs share the exact same synthetic node, so the
line is continuous by construction.

A pair is omitted only when the road path is more than MAX_RATIO times the
straight-line gap, which means the projection landed on the wrong road
entirely. A U-turn around a block is what the bus actually drives; drawing it
is honest, drawing a line through the block is not.
"""
from __future__ import annotations

import math
from pathlib import Path

import duckdb
import networkx as nx
import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "data" / "processed" / "reference.duckdb"
OUT = ROOT / "web-next" / "public" / "data"
GRAPH = ROOT / "data" / "processed" / "osm_delhi.graphml"

MAX_PAIR_M = 3000.0
# A road path this many times longer than the straight-line gap means the
# projection landed on the wrong road. The cap is deliberately loose: a bus
# crossing the Yamuna really does drive 9x the gap between its stops (High
# Court to Zoo is 217 m apart and 1,924 m by road), and rejecting that would
# delete a real route from the map.
MAX_RATIO = 40.0
# Candidate segments examined per station before the exact projection is taken.
SEG_CANDIDATES = 24


def metres(a, b) -> float:
    kx = 111320 * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((b[0] - a[0]) * kx, (b[1] - a[1]) * 110540)


def project_stations(
    stations: dict[int, tuple[float, float]],
    xs: np.ndarray,
    ys: np.ndarray,
    eu: np.ndarray,
    ev: np.ndarray,
) -> dict[int, tuple[int, int, float, float, float]]:
    """Project each station onto its nearest road segment.

    Returns station_id -> (u, v, t, lon, lat) where (lon, lat) is the point on
    segment u->v at parameter t.
    """
    mean_lat = float(ys.mean())
    kx = 111320 * math.cos(math.radians(mean_lat))

    ax, ay = xs[eu], ys[eu]
    bx, by = xs[ev], ys[ev]
    mid = np.column_stack(((ax + bx) / 2 * kx, (ay + by) / 2 * 110540))
    tree = cKDTree(mid)

    sids = list(stations)
    q = np.array([[stations[s][1] * kx, stations[s][0] * 110540] for s in sids])
    _, cand = tree.query(q, k=SEG_CANDIDATES)

    out: dict[int, tuple[int, int, float, float, float]] = {}
    for row, sid in enumerate(sids):
        px, py = q[row]
        best = (float("inf"), 0, 0.0)
        for j in cand[row]:
            j = int(j)
            dx = (bx[j] - ax[j]) * kx
            dy = (by[j] - ay[j]) * 110540
            L2 = dx * dx + dy * dy
            if L2 <= 0:
                continue
            t = ((px - ax[j] * kx) * dx + (py - ay[j] * 110540) * dy) / L2
            t = min(1.0, max(0.0, t))
            ox = ax[j] * kx + dx * t
            oy = ay[j] * 110540 + dy * t
            d = math.hypot(px - ox, py - oy)
            if d < best[0]:
                best = (d, j, t)
        d, j, t = best
        lon = float(ax[j] + (bx[j] - ax[j]) * t)
        lat = float(ay[j] + (by[j] - ay[j]) * t)
        out[sid] = (int(eu[j]), int(ev[j]), t, lon, lat)
    return out


def main() -> int:
    con = duckdb.connect(str(REF), read_only=True)
    stations = {
        int(sid): (float(lat), float(lon))
        for sid, lat, lon in con.execute(
            "select station_id, lat, lon from stations"
        ).fetchall()
    }
    route_pairs = [
        (int(a), int(b))
        for a, b in con.execute(
            """
            WITH seq AS (
              SELECT route_id, stop_sequence, station_id,
                     lead(station_id) OVER (PARTITION BY route_id ORDER BY stop_sequence) AS nxt
              FROM station_patterns
            )
            SELECT DISTINCT station_id, nxt FROM seq WHERE nxt IS NOT NULL
            ORDER BY 1, 2
            """
        ).fetchall()
    ]
    print(f"route pairs: {len(route_pairs):,}", flush=True)

    print(f"loading {GRAPH}", flush=True)
    G = nx.read_graphml(GRAPH)
    print(f"graph: {len(G.nodes):,} nodes, {len(G.edges):,} edges", flush=True)

    node_ids = list(G.nodes)
    index = {n: i for i, n in enumerate(node_ids)}
    xs = np.array([G.nodes[n]["x"] for n in node_ids], dtype=np.float64)
    ys = np.array([G.nodes[n]["y"] for n in node_ids], dtype=np.float64)

    eu = np.fromiter((index[u] for u, _ in G.edges()), dtype=np.int32, count=G.number_of_edges())
    ev = np.fromiter((index[v] for _, v in G.edges()), dtype=np.int32, count=G.number_of_edges())
    el = np.fromiter(
        (float(d.get("length", 0.0)) for _, _, d in G.edges(data=True)),
        dtype=np.float64,
        count=G.number_of_edges(),
    )
    print(f"edges indexed: {len(eu):,}", flush=True)

    print("projecting stations onto road segments", flush=True)
    proj = project_stations(stations, xs, ys, eu, ev)
    offs_m = np.array(
        [
            metres((stations[s][1], stations[s][0]), (proj[s][3], proj[s][4]))
            for s in stations
        ]
    )
    print(
        f"  stations: {len(proj):,}  offset median {np.median(offs_m):.1f} m  "
        f"p90 {np.percentile(offs_m, 90):.1f} m  max {offs_m.max():.1f} m",
        flush=True,
    )

    # Synthetic node per station, appended after the real nodes.
    base = len(node_ids)
    syn = {sid: base + i for i, sid in enumerate(sorted(proj))}
    syn_xy = np.array([[proj[s][3], proj[s][4]] for s in sorted(proj)], dtype=np.float64)

    all_x = np.concatenate([xs, syn_xy[:, 0]])
    all_y = np.concatenate([ys, syn_xy[:, 1]])

    # Two edges per synthetic node, one to each endpoint of its segment, in both
    # directions: a bus can enter or leave the road at that point either way.
    su, sv, sl = [], [], []
    for sid in sorted(proj):
        u, v, t, lon, lat = proj[sid]
        seg = metres((xs[u], ys[u]), (xs[v], ys[v]))
        n = syn[sid]
        su.extend((n, n, u, v))
        sv.extend((u, v, n, n))
        sl.extend((seg * t, seg * (1 - t), seg * t, seg * (1 - t)))

    su = np.concatenate([eu, np.array(su, dtype=np.int32)])
    sv = np.concatenate([ev, np.array(sv, dtype=np.int32)])
    sl = np.concatenate([el, np.array(sl, dtype=np.float64)])

    n_nodes = len(all_x)
    # Collapse parallel edges to the shortest one; csgraph needs a single weight.
    key = su.astype(np.int64) * n_nodes + sv
    order = np.lexsort((sl, key))
    key_s, su_s, sv_s, sl_s = key[order], su[order], sv[order], sl[order]
    first = np.ones(len(key_s), dtype=bool)
    first[1:] = key_s[1:] != key_s[:-1]
    su_s, sv_s, sl_s = su_s[first], sv_s[first], sl_s[first]
    print(f"csgraph edges: {len(su_s):,} (from {len(su):,})", flush=True)

    graph = csr_matrix((sl_s, (su_s, sv_s)), shape=(n_nodes, n_nodes))

    by_source: dict[int, list[tuple[int, int]]] = {}
    for i, (a, b) in enumerate(route_pairs):
        if a in syn and b in syn:
            by_source.setdefault(a, []).append((i, b))

    out_from = np.array([p[0] for p in route_pairs], dtype=np.int32)
    out_to = np.array([p[1] for p in route_pairs], dtype=np.int32)
    out_off = np.zeros(len(route_pairs) + 1, dtype=np.int32)
    out_xy: list[float] = []

    routed = no_path = bad_pair = 0
    ratios: list[float] = []
    done = 0
    for a in sorted(by_source):
        targets = by_source[a]
        dist, pred = dijkstra(
            graph, directed=True, indices=syn[a], return_predecessors=True
        )
        for i, b in targets:
            done += 1
            a_lat, a_lon = stations[a]
            b_lat, b_lon = stations[b]
            chord = metres((a_lon, a_lat), (b_lon, b_lat))
            if chord > MAX_PAIR_M:
                bad_pair += 1
                out_off[i + 1] = len(out_xy) // 2
                continue
            tgt = syn[b]
            if not np.isfinite(dist[tgt]):
                no_path += 1
                out_off[i + 1] = len(out_xy) // 2
                continue
            if chord > 0 and dist[tgt] / chord > MAX_RATIO:
                no_path += 1
                out_off[i + 1] = len(out_xy) // 2
                continue

            path = [tgt]
            cur = tgt
            while cur != syn[a]:
                cur = int(pred[cur])
                if cur < 0:
                    path = []
                    break
                path.append(cur)
            if len(path) < 2:
                no_path += 1
                out_off[i + 1] = len(out_xy) // 2
                continue
            path.reverse()

            for n in path:
                out_xy.extend((float(all_x[n]), float(all_y[n])))
            routed += 1
            if chord > 0:
                ratios.append(float(dist[tgt]) / chord)
            out_off[i + 1] = len(out_xy) // 2

        if done % 500 < len(targets):
            print(
                f"  {done}/{len(route_pairs)} routed={routed} no_path={no_path}",
                flush=True,
            )

    (OUT / "geom_from.bin").write_bytes(out_from.tobytes())
    (OUT / "geom_to.bin").write_bytes(out_to.tobytes())
    (OUT / "geom_off.bin").write_bytes(out_off.tobytes())
    (OUT / "geom_xy.bin").write_bytes(np.asarray(out_xy, dtype=np.float32).tobytes())

    r = np.array(ratios) if ratios else np.array([1.0])
    print(f"routed: {routed:,}  no path: {no_path:,}  bad pair: {bad_pair:,}", flush=True)
    print(
        f"ratio median {np.median(r):.2f}  p90 {np.percentile(r, 90):.2f}  "
        f"p99 {np.percentile(r, 99):.2f}  max {r.max():.2f}",
        flush=True,
    )
    print(
        f"points: {len(out_xy)//2:,}  bytes: {(OUT/'geom_xy.bin').stat().st_size/1e6:.2f} MB",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
