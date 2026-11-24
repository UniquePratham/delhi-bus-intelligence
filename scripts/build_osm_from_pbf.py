"""Build a routable Delhi road graph from the OSM India extract.

The Overpass API is unreachable from this network, so this reads the Geofabrik
India extract directly with pyosmium.

Two passes, both cheap:

  Pass 1: read ways only. Keep the drivable ones and record the node ids they
          reference.
  Pass 2: read nodes only. Keep the ones those ways need and that fall inside
          the Delhi bounding box.

No global location index and no bbox filter object, so it runs in minutes and
produces a real OSM network: shared junction nodes, one-way tags, road class.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import duckdb
import networkx as nx
import osmium

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "data" / "processed" / "reference.duckdb"
PBF = ROOT / "data" / "raw" / "india-latest.osm.pbf"
GRAPH = ROOT / "data" / "processed" / "osm_delhi.graphml"

DRIVABLE = {
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "unclassified", "residential", "service", "living_street",
    "motorway_link", "trunk_link", "primary_link", "secondary_link",
    "tertiary_link",
}
NO_ACCESS = {"private", "no", "customers", "delivery", "agricultural", "forestry"}


class WayPass(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.ways: list[tuple[list[int], dict]] = []
        self.needed: set[int] = set()

    def way(self, w):
        tags = w.tags
        if tags.get("highway") not in DRIVABLE:
            return
        if tags.get("access") in NO_ACCESS:
            return
        if tags.get("motor_vehicle") in NO_ACCESS:
            return
        nds = [nd.ref for nd in w.nodes]
        if len(nds) < 2:
            return
        self.ways.append((nds, dict(tags)))
        self.needed.update(nds)


class NodePass(osmium.SimpleHandler):
    def __init__(self, needed: set[int], bbox):
        super().__init__()
        self.needed = needed
        self.s, self.w, self.n, self.e = bbox
        self.nodes: dict[int, tuple[float, float]] = {}

    def node(self, n):
        if n.id not in self.needed:
            return
        lat, lon = n.location.lat, n.location.lon
        if self.s <= lat <= self.n and self.w <= lon <= self.e:
            self.nodes[n.id] = (lon, lat)


def main() -> int:
    ap = argparse.ArgumentParser()
    args = ap.parse_args()

    con = duckdb.connect(str(REF), read_only=True)
    lo_lat, lo_lon, hi_lat, hi_lon = con.execute(
        "select min(lat), min(lon), max(lat), max(lon) from stations"
    ).fetchone()
    pad = 0.01
    bbox = (lo_lat - pad, lo_lon - pad, hi_lat + pad, hi_lon + pad)
    print(f"bbox: {bbox}", flush=True)

    print("pass 1: ways", flush=True)
    wp = WayPass()
    wp.apply_file(str(PBF))
    print(f"  drivable ways: {len(wp.ways):,}  node refs: {len(wp.needed):,}", flush=True)

    print("pass 2: nodes", flush=True)
    npass = NodePass(wp.needed, bbox)
    npass.apply_file(str(PBF))
    print(f"  nodes in bbox: {len(npass.nodes):,}", flush=True)

    G = nx.MultiDiGraph()
    for nid, (lon, lat) in npass.nodes.items():
        G.add_node(nid, x=lon, y=lat)

    added = 0
    for nds, tags in wp.ways:
        oneway = tags.get("oneway", "no")
        forward = oneway != "-1"
        reverse = oneway not in ("yes", "true", "1", "-1")
        for a, b in zip(nds, nds[1:]):
            if a not in npass.nodes or b not in npass.nodes:
                continue
            (x1, y1), (x2, y2) = npass.nodes[a], npass.nodes[b]
            kx = 111320 * math.cos(math.radians((y1 + y2) / 2))
            length = math.hypot((x2 - x1) * kx, (y2 - y1) * 110540)
            if forward:
                G.add_edge(a, b, length=length, highway=tags.get("highway"))
            if reverse:
                G.add_edge(b, a, length=length, highway=tags.get("highway"))
            added += 1
    print(f"edges: {added:,}", flush=True)

    largest = max(nx.strongly_connected_components(G), key=len)
    G = G.subgraph(largest).copy()
    print(
        f"largest strongly connected component: "
        f"{len(G.nodes):,} nodes, {len(G.edges):,} edges",
        flush=True,
    )

    GRAPH.parent.mkdir(parents=True, exist_ok=True)
    nx.write_graphml(G, GRAPH)
    print(f"saved -> {GRAPH}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
