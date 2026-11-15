"""Evaluate the crowding model against known patterns.

There is no ground-truth occupancy in the supplied data, so this script does
not report an accuracy figure it cannot support. Instead it checks the model
against patterns that must hold if the reconstruction is sound:

1. **Peak hours are busier than off-peak.** Boardings must peak in the morning
   and evening and collapse overnight.
2. **Load rises then falls along a route.** A bus fills as it runs and empties
   toward the terminus.
3. **Weekdays are busier than weekends.**
4. **Week-to-week stability.** The same route/hour should look similar in
   different weeks, or the estimate is noise.
5. **Coverage.** How much of the network actually has evidence.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

from dbi import config


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(config.ARTIFACTS / "eval_crowding.json"))
    args = ap.parse_args()

    con = duckdb.connect(str(config.CROWDING_DB), read_only=True)
    con.execute("SET threads=8")

    res: dict = {}

    # 1. Hourly demand profile.
    hourly = con.execute(
        """
        SELECT hour, sum(boardings) AS boardings
        FROM crowding_demand GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    res["hourly_boardings"] = [{"hour": h, "boardings": int(b)} for h, b in hourly]
    peak = max(hourly, key=lambda r: r[1])
    trough = min(hourly, key=lambda r: r[1])
    res["peak_hour"] = {"hour": peak[0], "boardings": int(peak[1])}
    res["trough_hour"] = {"hour": trough[0], "boardings": int(trough[1])}
    res["peak_to_trough_ratio"] = round(peak[1] / max(trough[1], 1), 1)

    # 2. Load shape along a route: correlation between stop sequence and load.
    shape = con.execute(
        """
        WITH r AS (
          SELECT route, seq, avg(load_index) AS li
          FROM crowding_levels
          WHERE n_vehicle_days >= 5
          GROUP BY 1, 2
        ),
        c AS (
          SELECT route, corr(seq, li) AS rho, count(*) AS n
          FROM r GROUP BY 1 HAVING count(*) >= 8
        )
        SELECT count(*) AS routes,
               round(median(rho), 3) AS median_rho,
               round(100.0*sum(CASE WHEN rho > 0 THEN 1 ELSE 0 END)/count(*), 1) AS pct_rising
        FROM c
        """
    ).fetchone()
    res["load_shape"] = {
        "routes_tested": shape[0],
        "median_corr_seq_vs_load": shape[1],
        "pct_routes_rising": shape[2],
        "note": (
            "A positive correlation means load grows along the route, which is "
            "the expected boarding/alighting pattern."
        ),
    }

    # 3. Weekday vs weekend.
    dow = con.execute(
        """
        SELECT dow, sum(pax) AS boardings
        FROM tickets GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    weekday = sum(b for d, b in dow if d <= 4)
    weekend = sum(b for d, b in dow if d >= 5)
    res["weekday_vs_weekend"] = {
        "weekday_boardings": int(weekday),
        "weekend_boardings": int(weekend),
        "ratio": round(weekday / max(weekend, 1), 2),
        "note": "5 weekdays vs 2 weekend days, so the ratio is per-period totals.",
    }

    # 4. Week-to-week stability of the route/hour profile.
    stability = con.execute(
        """
        WITH wk AS (
          SELECT route, hour,
                 CAST(floor((day(service_date) - 1) / 7) AS INTEGER) AS week,
                 sum(pax) AS pax
          FROM tickets
          GROUP BY 1, 2, 3
        ),
        piv AS (
          SELECT route, hour,
                 max(CASE WHEN week = 0 THEN pax END) AS w0,
                 max(CASE WHEN week = 1 THEN pax END) AS w1,
                 max(CASE WHEN week = 2 THEN pax END) AS w2,
                 max(CASE WHEN week = 3 THEN pax END) AS w3
          FROM wk GROUP BY 1, 2
        )
        SELECT count(*) AS cells,
               round(corr(w0, w1), 3) AS r_w0_w1,
               round(corr(w1, w2), 3) AS r_w1_w2,
               round(corr(w2, w3), 3) AS r_w2_w3
        FROM piv
        WHERE w0 IS NOT NULL AND w1 IS NOT NULL AND w2 IS NOT NULL AND w3 IS NOT NULL
        """
    ).fetchone()
    res["week_to_week_stability"] = {
        "cells": stability[0],
        "corr_week0_week1": stability[1],
        "corr_week1_week2": stability[2],
        "corr_week2_week3": stability[3],
    }

    # 5. Coverage.
    cov = con.execute(
        """
        SELECT
          (SELECT count(DISTINCT route) FROM crowding_levels) AS routes_with_crowding,
          (SELECT count(DISTINCT route) FROM route_link) AS routes_total,
          (SELECT count(*) FROM crowding_levels) AS cells,
          (SELECT count(*) FROM crowding_levels WHERE confidence = 'high') AS high_conf,
          (SELECT count(*) FROM crowding_levels WHERE confidence = 'medium') AS med_conf,
          (SELECT count(*) FROM crowding_levels WHERE confidence = 'low') AS low_conf
        """
    ).fetchone()
    res["coverage"] = {
        "routes_with_crowding": cov[0],
        "routes_total": cov[1],
        "route_coverage_pct": round(100.0 * cov[0] / max(cov[1], 1), 1),
        "cells": cov[2],
        "confidence_high": cov[3],
        "confidence_medium": cov[4],
        "confidence_low": cov[5],
    }

    # 6. Category distribution.
    cats = con.execute(
        """
        SELECT crowding_band, count(*) AS n
        FROM crowding_levels GROUP BY 1 ORDER BY n DESC
        """
    ).fetchall()
    total = sum(c[1] for c in cats)
    res["category_distribution"] = [
        {"category": c[0], "n": c[1], "pct": round(100.0 * c[1] / total, 1)}
        for c in cats
    ]

    con.close()

    p = Path(args.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(res, indent=2), encoding="utf-8")
    print(json.dumps(res, indent=2))
    print(f"\nwrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
