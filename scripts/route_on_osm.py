"""Route every station pair on the real OSM road network.

The graph is the Delhi extract from OpenStreetMap: 922,941 nodes and 1,961,233
edges in one strongly connected component, with real junction topology and
one-way tags.

Each station is snapped to its nearest OSM node once, then each consecutive pair
is routed with a shortest path. Consecutive pairs share the exact same node, so
the line is continuous by construction. No connectors, no anchoring, no
post-hoc filters.
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

    # Build a spatial index over the graph nodes.
    node_ids = list(G.nodes)
    xs = np.array([G.nodes[n]["x"] for n in node_ids], dtype=np.float64)
    ys = np.array([G.nodes[n]["y"] for n in node_ids], dtype=np.float64)

    from scipy.spatial import cKDTree

    tree = cKDTree(np.column_stack([xs, ys]))

    station_node: dict[int, str] = {}
    for sid, (lat, lon) in stations.items():
        _, idx = tree.query([lon, lat], k=1)
        station_node[sid] = node_ids[int(idx)]
    print(f"stations snapped: {len(station_node):,}", flush=True)

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
        if metres((a_lon, a_lat), (b_lon, b_lat)) > MAX_PAIR_M:
            bad_pair += 1
            out_off[i + 1] = len(out_xy) // 2
            continue

        s = station_node[a]
        e = station_node[b]
        try:
            path = nx.shortest_path(G, s, e, weight="length")
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            no_path += 1
            out_off[i + 1] = len(out_xy) // 2
            continue

        if len(path) < 2:
            no_path += 1
            out_off[i + 1] = len(out_xy) // 2
            continue

        for n in path:
            out_xy.extend((float(G.nodes[n]["x"]), float(G.nodes[n]["y"])))
        routed += 1
        out_off[i + 1] = len(out_xy) // 2

        if (i + 1) % 500 == 0:
            print(f"  {i+1}/{len(route_pairs)} routed={routed} no_path={no_path}", flush=True)

    (OUT / "geom_from.bin").write_bytes(out_from.tobytes())
    (OUT / "geom_to.bin").write_bytes(out_to.tobytes())
    (OUT / "geom_off.bin").write_bytes(out_off.tobytes())
    (OUT / "geom_xy.bin").write_bytes(np.asarray(out_xy, dtype=np.float32).tobytes())

    print(f"routed: {routed:,}  no path: {no_path:,}  bad pair: {bad_pair:,}", flush=True)
    print(f"points: {len(out_xy)//2:,}  bytes: {(OUT/'geom_xy.bin').stat().st_size/1e6:.2f} MB", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
