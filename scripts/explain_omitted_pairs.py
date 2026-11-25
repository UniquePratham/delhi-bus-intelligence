"""Explain every station pair the router omitted.

A pair is omitted for one of three reasons, and the map should say which:

  far      the two stations are more than MAX_PAIR_M apart, so they are not a
           real consecutive stop pair (a GTFS artefact, not a routing failure)
  no-path  the road graph has no directed path between the projected points
  ratio    a path exists but is more than MAX_RATIO times the straight-line gap

Reporting the split matters: "177 pairs omitted" sounds like a routing failure,
while "154 are not real consecutive stops and 23 have no road path" is a
statement about the data.
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
GRAPH = ROOT / "data" / "processed" / "osm_delhi.graphml"
OUT = ROOT / "web-next" / "public" / "data"

MAX_PAIR_M = 3000.0
MAX_RATIO = 40.0
SEG_CANDIDATES = 24


def metres(a, b):
    kx = 111320 * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((b[0] - a[0]) * kx, (b[1] - a[1]) * 110540)


def main() -> int:
    con = duckdb.connect(str(REF), read_only=True)
    stations = {
        int(sid): (float(lat), float(lon))
        for sid, lat, lon in con.execute(
            "select station_id, lat, lon from stations"
        ).fetchall()
    }
    names = {
        int(sid): n
        for sid, n in con.execute("select station_id, name from stations").fetchall()
    }
    pairs = [
        (int(a), int(b))
        for a, b in con.execute(
            """
            WITH seq AS (
              SELECT route_id, stop_sequence, station_id,
                     lead(station_id) OVER (PARTITION BY route_id ORDER BY stop_sequence) AS nxt
              FROM station_patterns
            )
            SELECT DISTINCT station_id, nxt FROM seq WHERE nxt IS NOT NULL ORDER BY 1, 2
            """
        ).fetchall()
    ]

    froms = np.fromfile(OUT / "geom_from.bin", dtype=np.int32)
    tos = np.fromfile(OUT / "geom_to.bin", dtype=np.int32)
    offs = np.fromfile(OUT / "geom_off.bin", dtype=np.int32)
    have = {(int(froms[i]), int(tos[i])) for i in range(len(froms)) if offs[i + 1] > offs[i]}
    missing = [p for p in pairs if p not in have]
    print(f"pairs: {len(pairs):,}  drawn: {len(have):,}  omitted: {len(missing):,}")

    far = [p for p in missing if metres(
        (stations[p[0]][1], stations[p[0]][0]),
        (stations[p[1]][1], stations[p[1]][0]),
    ) > MAX_PAIR_M]
    print(f"\n  more than {MAX_PAIR_M:.0f} m apart (not a real consecutive stop): {len(far):,}")
    for a, b in far[:5]:
        d = metres((stations[a][1], stations[a][0]), (stations[b][1], stations[b][0]))
        print(f"    {a} ({names.get(a)}) -> {b} ({names.get(b)}): {d/1000:.1f} km")

    rest = [p for p in missing if p not in set(far)]
    print(f"\n  within {MAX_PAIR_M:.0f} m but still omitted: {len(rest):,}")

    # Rebuild the routing graph exactly as route_on_osm.py does, so the reason
    # for each remaining omission can be stated rather than guessed.
    G = nx.read_graphml(GRAPH)
    ids = list(G.nodes)
    index = {n: i for i, n in enumerate(ids)}
    xs = np.array([G.nodes[n]["x"] for n in ids])
    ys = np.array([G.nodes[n]["y"] for n in ids])
    eu = np.fromiter((index[u] for u, _ in G.edges()), dtype=np.int32, count=G.number_of_edges())
    ev = np.fromiter((index[v] for _, v in G.edges()), dtype=np.int32, count=G.number_of_edges())
    el = np.fromiter((float(d.get("length", 0.0)) for _, _, d in G.edges(data=True)),
                     dtype=np.float64, count=G.number_of_edges())

    mean_lat = float(ys.mean())
    kx = 111320 * math.cos(math.radians(mean_lat))
    ax, ay = xs[eu], ys[eu]
    bx, by = xs[ev], ys[ev]
    mid = np.column_stack(((ax + bx) / 2 * kx, (ay + by) / 2 * 110540))
    tree = cKDTree(mid)

    def project(sid):
        lat, lon = stations[sid]
        px, py = lon * kx, lat * 110540
        _, cand = tree.query([px, py], k=SEG_CANDIDATES)
        best = (float("inf"), 0, 0.0)
        for j in cand:
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
        return best

    base = len(ids)
    syn = {}
    su, sv, sl = [], [], []
    for sid in stations:
        d, j, t = project(sid)
        n = base + len(syn)
        syn[sid] = n
        seg = metres((xs[eu[j]], ys[eu[j]]), (xs[ev[j]], ys[ev[j]]))
        su.extend((n, n, int(eu[j]), int(ev[j])))
        sv.extend((int(eu[j]), int(ev[j]), n, n))
        sl.extend((seg * t, seg * (1 - t), seg * t, seg * (1 - t)))

    su = np.concatenate([eu, np.array(su, dtype=np.int32)])
    sv = np.concatenate([ev, np.array(sv, dtype=np.int32)])
    sl = np.concatenate([el, np.array(sl, dtype=np.float64)])
    n_nodes = base + len(syn)
    key = su.astype(np.int64) * n_nodes + sv
    order = np.lexsort((sl, key))
    key_s, su_s, sv_s, sl_s = key[order], su[order], sv[order], sl[order]
    first = np.ones(len(key_s), dtype=bool)
    first[1:] = key_s[1:] != key_s[:-1]
    graph = csr_matrix((sl_s[first], (su_s[first], sv_s[first])), shape=(n_nodes, n_nodes))

    no_path = []
    over_ratio = []
    for a, b in rest:
        dist = dijkstra(graph, directed=True, indices=syn[a])
        d = float(dist[syn[b]])
        chord = metres((stations[a][1], stations[a][0]), (stations[b][1], stations[b][0]))
        if not np.isfinite(d):
            no_path.append((a, b, chord))
        elif chord > 0 and d / chord > MAX_RATIO:
            over_ratio.append((a, b, chord, d))

    print(f"\n  no directed road path: {len(no_path):,}")
    for a, b, chord in no_path[:10]:
        print(f"    {a} ({names.get(a)}) -> {b} ({names.get(b)}): {chord:.0f} m apart")
    print(f"\n  path longer than {MAX_RATIO:.0f}x the gap: {len(over_ratio):,}")
    for a, b, chord, d in over_ratio[:10]:
        print(f"    {a} ({names.get(a)}) -> {b} ({names.get(b)}): "
              f"{chord:.0f} m apart, {d:.0f} m by road ({d/chord:.1f}x)")

    # Are these all same-place platform pairs? If so they are a GTFS artefact
    # (two platforms of one stop listed as consecutive), not a routing failure.
    def norm(s: str) -> str:
        return "".join(c for c in s.lower() if c.isalnum())

    same = 0
    close = 0
    for a, b, chord, d in over_ratio:
        na, nb = norm(names.get(a, "")), norm(names.get(b, ""))
        if na and nb and (na == nb or na in nb or nb in na):
            same += 1
        if chord <= 400:
            close += 1
    print(f"\n  of those, the two stations share a name: {same:,} / {len(over_ratio):,}")
    print(f"  of those, the two stations are within 400 m: {close:,} / {len(over_ratio):,}")
    print(f"  max straight-line gap among them: "
          f"{max((c for _, _, c, _ in over_ratio), default=0):.0f} m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
