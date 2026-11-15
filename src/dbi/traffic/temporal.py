"""Check that the model distinguishes weekday from weekend, and peak from night.

This is the evidence that the extra days did real work. It compares the model's
own predictions for the same station pair across day types and hours, and
reports how often the ordering matches expectation.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbi import config  # noqa: E402
from dbi.journey.planner import Planner  # noqa: E402

# 2026-10-05 is a Monday, 2026-10-10 a Saturday, 2026-10-11 a Sunday.
MONDAY = datetime(2026, 10, 5, 9, 0)
SATURDAY = datetime(2026, 10, 10, 9, 0)
SUNDAY = datetime(2026, 10, 11, 9, 0)
MONDAY_NIGHT = datetime(2026, 10, 5, 2, 0)


def main() -> int:
    p = Planner()
    pairs = list(p.pair_hour.keys())
    sample = [(f, t) for (f, t, _how) in pairs][:4000]

    def compare(a: datetime, b: datetime, label: str) -> dict:
        slower = faster = same = 0
        for frm, to in sample:
            ta, _ = p.travel_time(frm, to, a)
            tb, _ = p.travel_time(frm, to, b)
            if ta > tb * 1.02:
                slower += 1
            elif tb > ta * 1.02:
                faster += 1
            else:
                same += 1
        total = slower + faster + same
        return {
            "comparison": label,
            "n": total,
            "first_slower_pct": round(100.0 * slower / total, 1),
            "second_slower_pct": round(100.0 * faster / total, 1),
            "same_pct": round(100.0 * same / total, 1),
        }

    results = [
        compare(MONDAY, MONDAY_NIGHT, "Monday 09:00 vs Monday 02:00"),
        compare(MONDAY, SATURDAY, "Monday 09:00 vs Saturday 09:00"),
        compare(MONDAY, SUNDAY, "Monday 09:00 vs Sunday 09:00"),
    ]

    # How many distinct hour-of-week profiles exist, and for which weekdays.
    con_pairs = p.pair_hour
    dows = sorted({how // 24 for (_f, _t, how) in con_pairs})
    out = {
        "distinct_hour_of_week_values": len({how for (_f, _t, how) in con_pairs}),
        "weekdays_present": dows,
        "weekday_names": [
            ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][d] for d in dows
        ],
        "comparisons": results,
    }
    print(json.dumps(out, indent=2))

    path = config.ARTIFACTS / "eval_temporal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
