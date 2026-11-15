"""Verify the system degrades gracefully when the crowding module is absent.

The brief requires that the journey planner still works if crowding data is
unavailable. This test proves it rather than asserting it.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbi.journey.planner import Planner  # noqa: E402


def main() -> int:
    o = None
    d = None

    print("== with crowding ==")
    p = Planner()
    o = p.resolve_place(name="Kashmere Gate ISBT")
    d = p.resolve_place(name="Nehru Place")
    js = p.plan((o[0], o[1]), (d[0], d[1]), datetime(2026, 10, 15, 8, 30))
    for j in js:
        p.enrich_crowding(j)
    known = sum(
        1
        for j in js
        for leg in j.bus_legs
        if leg.crowding and leg.crowding["category"] != "unknown"
    )
    print(f"  journeys={len(js)} legs_with_crowding={known}")

    print("\n== with crowding DB missing ==")
    p2 = Planner(crowding_db=Path("data/processed/does_not_exist.duckdb"))
    js2 = p2.plan((o[0], o[1]), (d[0], d[1]), datetime(2026, 10, 15, 8, 30))
    for j in js2:
        p2.enrich_crowding(j)
    print(f"  journeys={len(js2)}")
    for j in js2:
        cats = [leg.crowding["category"] if leg.crowding else None for leg in j.bus_legs]
        print(f"    {j.duration_s/60:.1f}min crowding={cats}")

    ok = len(js2) > 0 and all(
        leg.crowding is None or leg.crowding["category"] == "unknown"
        for j in js2
        for leg in j.bus_legs
    )
    print(f"\n{'PASS' if ok else 'FAIL'}: planner works without crowding data")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
