"""Normalise route identifiers so ticket routes can be linked to GTFS routes.

Ticket route strings and GTFS `route_long_name` values are written by different
systems and differ in ways that are systematic, not random:

    ticket "874EUP"      vs GTFS "874UP"        (variant letter)
    ticket "D-034UP"     vs GTFS "34UP"         (depot prefix, zero padding)
    ticket "801-IGI-INDDOWN" vs GTFS "801DOWN"  (corridor spelled out)
    ticket "OMS(+)BURARI-DHAULAKUAN"            (no numeric route at all)

The normaliser extracts a *route key*: the leading numeric part plus the
direction. That is deliberately conservative — it never invents a route that
does not exist, and it is only used as a fallback when the exact name fails.
"""
from __future__ import annotations

import re

DIRECTION_RE = re.compile(r"(UP|DOWN)$")


def split_direction(route: str) -> tuple[str, str]:
    """Return (base, direction) where direction is UP, DOWN or NA."""
    r = (route or "").strip().upper()
    m = DIRECTION_RE.search(r)
    if not m:
        return r, "NA"
    return r[: m.start()], m.group(1)


def route_key(route: str) -> str | None:
    """A coarse key: leading digits + direction, or None if there are none.

    "874EUP"   -> "874UP"
    "D-034UP"  -> "34UP"
    "801-IGI-INDDOWN" -> "801DOWN"
    "OMS(+)BURARI-DHAULAKUAN" -> None
    """
    base, direction = split_direction(route)
    if direction == "NA":
        return None
    digits = re.match(r"^[A-Z]*-?0*(\d+)", base)
    if not digits:
        return None
    return f"{digits.group(1)}{direction}"


def route_keys(route: str) -> list[str]:
    """Keys to try, most specific first."""
    out: list[str] = []
    base, direction = split_direction(route)
    if direction != "NA":
        # Exact base, then base with a trailing variant letter removed.
        out.append(f"{base}{direction}")
        stripped = re.sub(r"[A-Z]$", "", base)
        if stripped and stripped != base:
            out.append(f"{stripped}{direction}")
    key = route_key(route)
    if key:
        out.append(key)
    seen: set[str] = set()
    uniq: list[str] = []
    for k in out:
        if k and k not in seen:
            seen.add(k)
            uniq.append(k)
    return uniq
