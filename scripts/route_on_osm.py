"""Route every station pair on the real OSM road network.

The graph is the Delhi extract from OpenStreetMap: 922,941 nodes and 1,961,233
edges in one strongly connected component, with real junction topology and
one-way tags.

Two refinements over a naive single-node snap:

  * Every OSM node within NEAR_M of a station is a candidate, not just the
    nearest one. A stop on a divided road has nodes on both carriageways, and
    picking the right one avoids a U-turn.
  * A route longer than MAX_RATIO times the straight-line distance is rejected.
    That is the signature of a U-turn around a block, and drawing it is worse
    than omitting the pair.

Consecutive pairs share the same candidate set, so the line stays continuous.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import duckdb
import networkx as nx
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "data" / "processed" / "reference.duckdb"
OUT = ROOT / "web-next" / "public" / "data"
GRAPH = ROOT / "data" / "processed" / "osm_delhi.graphml"

MAX_PAIR_M = 3000.0
NEAR_M = 120.0
# A route this many times longer than the straight-line distance is a U-turn
# worth searching alternatives for. Genuine round trips are kept: a bus that
# goes around a block really does travel further than the gap between its stops.
UTURN_RATIO = 2.0
# Cap the alternative search so a pair cannot take minutes.
MAX_CANDS = 8
# Only reject a route that is wildly longer than the gap, which means the snap
# picked the wrong road entirely.
MAX_RATIO = 8.0


def metres(a, b) -> float:
    kx = 111320 * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((b[0] - a[0]) * kx, (b[1] - a[1]) * 110540)


def main() -> int:
    ap = argparse.ArgumentParser()
    args = ap.parse_args()

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
    xs = np.array([G.nodes[n]["x"] for n in node_ids], dtype=np.float64)
    ys = np.array([G.nodes[n]["y"] for n in node_ids], dtype=np.float64)

    from scipy.spatial import cKDTree

    tree = cKDTree(np.column_stack([xs, ys]))

    station_cands: dict[int, list[str]] = {}
    for sid, (lat, lon) in stations.items():
        idxs = tree.query_ball_point([lon, lat], r=NEAR_M / 111320.0)
        if not idxs:
            _, idx = tree.query([lon, lat], k=1)
            idxs = [int(idx)]
        station_cands[sid] = [node_ids[int(i)] for i in idxs]
    print(
        f"stations: {len(station_cands):,}  "
        f"mean candidates: {sum(len(v) for v in station_cands.values())/len(station_cands):.1f}",
        flush=True,
    )

    out_from = np.empty(len(route_pairs), dtype=np.int32)
    out_to = np.empty(len(route_pairs), dtype=np.int32)
    out_off = np.zeros(len(route_pairs) + 1, dtype=np.int32)
    out_xy: list[float] = []

    routed = no_path = bad_pair = 0
    for i, (a, b) in enumerate(route_pairs):
        out_from[i] = a
        out_to[i] = b

        a_lat, a_lon = stations[a]
        b_lat, b_lon = stations[b]
        chord = metres((a_lon, a_lat), (b_lon, b_lat))
        if chord > MAX_PAIR_M:
            bad_pair += 1
            out_off[i + 1] = len(out_xy) // 2
            continue

        best_path = None
        best_len = float("inf")

        # Use one canonical node per station, chosen once, so consecutive pairs
        # share the exact same endpoint and the line is continuous. The
        # canonical node is the nearest one, which is what a bus stop is.
        s0 = station_cands[a][0]
        e0 = station_cands[b][0]
        try:
            p0 = nx.shortest_path(G, s0, e0, weight="length")
            best_path = p0
            best_len = sum(
                metres(
                    (float(G.nodes[p0[j]]["x"]), float(G.nodes[p0[j]]["y"])),
                    (float(G.nodes[p0[j + 1]]["x"]), float(G.nodes[p0[j + 1]]["y"])),
                )
                for j in range(len(p0) - 1)
            )
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            pass

        # If the canonical route is a U-turn, try the other candidates. The
        # result is still anchored at the canonical nodes at both ends, so the
        # line stays continuous: only the middle of the path changes.
        if best_path is None or (chord > 0 and best_len / chord > UTURN_RATIO):
            for s in station_cands[a][:MAX_CANDS]:
                for e in station_cands[b][:MAX_CANDS]:
                    if s == e:
                        continue
                    try:
                        p = nx.shortest_path(G, s, e, weight="length")
                    except (nx.NetworkXNoPath, nx.NodeNotFound):
                        continue
                    L = sum(
                        metres(
                            (float(G.nodes[p[j]]["x"]), float(G.nodes[p[j]]["y"])),
                            (float(G.nodes[p[j + 1]]["x"]), float(G.nodes[p[j + 1]]["y"])),
                        )
                        for j in range(len(p) - 1)
                    )
                    if L < best_len:
                        best_len = L
                        best_path = p

        if best_path is None or len(best_path) < 2:
            no_path += 1
            out_off[i + 1] = len(out_xy) // 2
            continue

        if chord > 0 and best_len / chord > MAX_RATIO:
            # Only reject a route that is wildly longer than the gap, which
            # means the snap picked the wrong road. A genuine round trip around
            # a block is kept.
            no_path += 1
            out_off[i + 1] = len(out_xy) // 2
            continue

        # Anchor both ends to the station's canonical node so consecutive pairs
        # share an exact endpoint. Without this, a pair that used an alternative
        # candidate ends somewhere the next pair does not start, and the line
        # breaks.
        if best_path[0] != s0:
            best_path = [s0] + best_path
        if best_path[-1] != e0:
            best_path = best_path + [e0]

        for n in best_path:
            out_xy.extend((float(G.nodes[n]["x"]), float(G.nodes[n]["y"])))
        routed += 1
        out_off[i + 1] = len(out_xy) // 2

        if (i + 1) % 500 == 0:
            print(
                f"  {i+1}/{len(route_pairs)} routed={routed} no_path={no_path}",
                flush=True,
            )

    (OUT / "geom_from.bin").write_bytes(out_from.tobytes())
    (OUT / "geom_to.bin").write_bytes(out_to.tobytes())
    (OUT / "geom_off.bin").write_bytes(out_off.tobytes())
    (OUT / "geom_xy.bin").write_bytes(np.asarray(out_xy, dtype=np.float32).tobytes())

    print(f"routed: {routed:,}  no path: {no_path:,}  bad pair: {bad_pair:,}", flush=True)
    print(f"points: {len(out_xy)//2:,}  bytes: {(OUT/'geom_xy.bin').stat().st_size/1e6:.2f} MB", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
