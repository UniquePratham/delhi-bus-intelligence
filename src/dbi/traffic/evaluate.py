"""Evaluate the travel-time model on the held-out day.

Baselines compared
------------------
1. ``schedule``      — the GTFS timetable (constant-speed assumption).
2. ``global_median`` — one number for every pair, all hours.
3. ``pair_median``   — per-pair median, ignoring time of day.
4. ``model``         — the shrinkage hierarchy (pair x hour-of-week first).

Metrics are reported per station-pair run, which is the unit the planner
actually consumes. MAE and RMSE are in seconds; a percentage error is also
reported because a 30 s error on a 60 s run is not the same as on a 600 s run.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

from dbi import config


def evaluate(db: Path) -> dict:
    con = duckdb.connect(str(db), read_only=True)
    con.execute("SET threads=8")

    # Predictions on the held-out day, with the hierarchy level that fired.
    con.execute(
        """
        CREATE TEMP TABLE pred AS
        SELECT e.from_station, e.to_station, e.hour_of_week, e.hour_band,
               e.travel_s AS actual,
               ph.travel_s AS p_pair_hour,
               pb.travel_s AS p_pair_band,
               pp.travel_s AS p_pair,
               gb.travel_s AS p_global_band,
               gg.travel_s AS p_global,
               s.scheduled_s AS p_schedule
        FROM eval_runs e
        LEFT JOIN profile_pair_hour ph
          ON ph.from_station = e.from_station AND ph.to_station = e.to_station
         AND ph.hour_of_week = e.hour_of_week
        LEFT JOIN profile_pair_band pb
          ON pb.from_station = e.from_station AND pb.to_station = e.to_station
         AND pb.hour_band = e.hour_band
        LEFT JOIN profile_pair pp
          ON pp.from_station = e.from_station AND pp.to_station = e.to_station
        LEFT JOIN profile_global_band gb ON gb.hour_band = e.hour_band
        CROSS JOIN profile_global gg
        LEFT JOIN schedule s
          ON s.from_station = e.from_station AND s.to_station = e.to_station
        """
    )

    # The model prediction is the first level that has enough evidence.
    # MIN_SUPPORT guards against a single observation defining an hour.
    con.execute(
        """
        CREATE TEMP TABLE scored AS
        SELECT *,
               coalesce(p_pair_hour, p_pair_band, p_pair, p_global_band, p_global) AS p_model,
               CASE
                 WHEN p_pair_hour IS NOT NULL THEN 'pair_hour'
                 WHEN p_pair_band IS NOT NULL THEN 'pair_band'
                 WHEN p_pair IS NOT NULL THEN 'pair'
                 WHEN p_global_band IS NOT NULL THEN 'global_band'
                 ELSE 'global'
               END AS level
        FROM pred
        """
    )

    def metrics(col: str) -> dict:
        row = con.execute(
            f"""
            SELECT count({col}) AS n,
                   avg(abs({col} - actual)) AS mae,
                   sqrt(avg(pow({col} - actual, 2))) AS rmse,
                   median(abs({col} - actual)) AS med_ae,
                   avg(abs({col} - actual) / nullif(actual, 0)) * 100 AS mape,
                   avg({col} - actual) AS bias
            FROM scored WHERE {col} IS NOT NULL
            """
        ).fetchone()
        return {
            "n": int(row[0]),
            "mae_s": round(row[1], 2),
            "rmse_s": round(row[2], 2),
            "median_ae_s": round(row[3], 2),
            "mape_pct": round(row[4], 2),
            "bias_s": round(row[5], 2),
        }

    results = {
        "holdout_day": con.execute(
            "SELECT max(CAST(depart_ist AS DATE))::VARCHAR FROM eval_runs"
        ).fetchone()[0],
        "n_eval_runs": con.execute("SELECT count(*) FROM eval_runs").fetchone()[0],
        "baselines": {
            "schedule": metrics("p_schedule"),
            "global_median": metrics("p_global"),
            "pair_median": metrics("p_pair"),
            "model": metrics("p_model"),
        },
        "model_level_usage": {
            r[0]: {"n": int(r[1]), "pct": round(r[2], 1)}
            for r in con.execute(
                """
                SELECT level, count(*), 100.0*count(*)/sum(count(*)) OVER ()
                FROM scored GROUP BY 1 ORDER BY 2 DESC
                """
            ).fetchall()
        },
    }

    # Accuracy by hour band, to show the model tracks the daily cycle.
    results["by_hour_band"] = [
        {
            "hour_band": r[0],
            "n": int(r[1]),
            "model_mae_s": round(r[2], 2),
            "schedule_mae_s": round(r[3], 2) if r[3] is not None else None,
        }
        for r in con.execute(
            """
            SELECT hour_band, count(*),
                   avg(abs(p_model - actual)),
                   avg(abs(p_schedule - actual))
            FROM scored GROUP BY 1 ORDER BY 1
            """
        ).fetchall()
    ]

    con.close()
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(config.TRAFFIC_DB))
    ap.add_argument("--out", default=str(config.ARTIFACTS / "eval_traffic.json"))
    args = ap.parse_args()

    res = evaluate(Path(args.db))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=2), encoding="utf-8")
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
