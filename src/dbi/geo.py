"""Small geospatial helpers shared by the pipelines."""
from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def haversine_sql(lat1: str, lon1: str, lat2: str, lon2: str) -> str:
    """DuckDB expression for haversine distance in metres."""
    return (
        f"2*{EARTH_RADIUS_M}*asin(sqrt("
        f"pow(sin(radians({lat2}-{lat1})/2),2) + "
        f"cos(radians({lat1}))*cos(radians({lat2}))*"
        f"pow(sin(radians({lon2}-{lon1})/2),2)))"
    )


def point_segment_distance_m(
    plat: float, plon: float, alat: float, alon: float, blat: float, blon: float
) -> tuple[float, float]:
    """Distance from a point to a segment, plus the fraction along the segment.

    Works in a local equirectangular projection, which is accurate to well
    under a metre over the few-hundred-metre segments used here.
    """
    kx = math.cos(math.radians(plat)) * 111_320.0
    ky = 110_540.0
    ax, ay = (alon - plon) * kx, (alat - plat) * ky
    bx, by = (blon - plon) * kx, (blat - plat) * ky
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    if denom == 0.0:
        return math.hypot(ax, ay), 0.0
    t = -(ax * dx + ay * dy) / denom
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(cx, cy), t
