"""Build the time-dependent travel-time model.

Model
-----
For each directed station pair the model predicts run time as a function of
hour of week, using a shrinkage hierarchy so sparse pairs fall back to broader
evidence instead of returning noise:

    pair x hour-of-week -> pair x hour-band -> pair (all hours)
                        -> global hour-band -> global

Each level is a median of observed runs. The level actually used is recorded
per prediction so the API can report confidence honestly.

Leakage control
---------------
Profiles are built from the *training* days only. The final day is held out
and used solely for evaluation, so no reported metric has seen its own answer.

Live/historical fusion
----------------------
Historical profiles are the prior. Recent observations are blended in with a
weight that grows with the number of observations, so a pair with two fresh
runs cannot override a month of history:

    w_live = n_live / (n_live + K)
    estimate = (1 - w_live) * historical + w_live * live_median
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

from dbi import config

HOUR_BANDS = [
    (0, 5, "night"),
    (6, 7, "early_peak"),
    (8, 11, "morning_peak"),
    (12, 16, "midday"),
    (17, 20, "evening_peak"),
    (21, 23, "late"),
]

LIVE_SHRINK_K = 8.0


def _band_case(col: str) -> str:
    whens = " ".join(
        f"WHEN {col} BETWEEN {lo} AND {hi} THEN '{name}'" for lo, hi, name in HOUR_BANDS
    )
    return f"CASE {whens} ELSE 'late' END"


def _add_time_cols(con: duckdb.DuckDBPyConnection, table: str) -> None:
    # hour_of_week = weekday * 24 + hour, where weekday is 0=Monday, matching
    # Python's datetime.weekday() used by the planner.
    #
    # DuckDB's extract(dow) is 0=Sunday, so it must be converted:
    #   (dow + 6) % 7  ->  0=Monday .. 6=Sunday
    # Getting this wrong shifts every lookup by one day, which silently
    # serves Monday's profile for a Tuesday query.
    #
    # hour_of_week ranges 0..167, so it needs SMALLINT; TINYINT overflows at
    # 127 once all seven weekdays are present.
    con.execute(
        f"""
        ALTER TABLE {table} ADD COLUMN hour TINYINT;
        ALTER TABLE {table} ADD COLUMN dow TINYINT;
        ALTER TABLE {table} ADD COLUMN hour_band VARCHAR;
        ALTER TABLE {table} ADD COLUMN hour_of_week SMALLINT;
        UPDATE {table} SET
          hour = CAST(extract(hour FROM depart_ist) AS TINYINT),
          dow  = CAST((CAST(extract(dow FROM depart_ist) AS INTEGER) + 6) % 7 AS TINYINT);
        UPDATE {table} SET hour_band = {_band_case('hour')};
        UPDATE {table} SET hour_of_week = CAST(CAST(dow AS SMALLINT) * 24 + hour AS SMALLINT);
        """
    )


def _build_profiles(con: duckdb.DuckDBPyConnection, src: str) -> None:
    con.execute(
        f"""
        CREATE OR REPLACE TABLE profile_pair_hour AS
        SELECT from_station, to_station, hour_of_week,
               median(travel_s) AS travel_s, count(*) AS n,
               quantile_cont(travel_s, 0.25) AS p25,
               quantile_cont(travel_s, 0.75) AS p75
        FROM {src} GROUP BY 1, 2, 3
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE profile_pair_band AS
        SELECT from_station, to_station, hour_band,
               median(travel_s) AS travel_s, count(*) AS n,
               quantile_cont(travel_s, 0.25) AS p25,
               quantile_cont(travel_s, 0.75) AS p75
        FROM {src} GROUP BY 1, 2, 3
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE profile_pair AS
        SELECT from_station, to_station,
               median(travel_s) AS travel_s, count(*) AS n,
               quantile_cont(travel_s, 0.25) AS p25,
               quantile_cont(travel_s, 0.75) AS p75
        FROM {src} GROUP BY 1, 2
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE profile_global_band AS
        SELECT hour_band, median(travel_s) AS travel_s, count(*) AS n
        FROM {src} GROUP BY 1
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE profile_global AS
        SELECT median(travel_s) AS travel_s, count(*) AS n FROM {src}
        """
    )


def build(dates: list[str], out_db: Path) -> None:
    out_db.parent.mkdir(parents=True, exist_ok=True)
    if out_db.exists():
        out_db.unlink()

    con = duckdb.connect(str(out_db))
    con.execute("SET threads=8")
    con.execute("SET memory_limit='6GB'")

    globs = [str(config.PROCESSED / "gps" / f"date={d}" / "station_runs.parquet") for d in dates]
    for g in globs:
        if not Path(g).exists():
            raise SystemExit(f"missing {g}")

    con.execute(f"CREATE TABLE runs AS SELECT * FROM read_parquet({globs!r})")
    _add_time_cols(con, "runs")
    print(f"runs loaded: {con.execute('SELECT count(*) FROM runs').fetchone()[0]:,}")

    # Chronological holdout: last day is never used to fit anything.
    test_date = dates[-1]
    iso = f"{test_date[:4]}-{test_date[5:7]}-{test_date[8:10]}"
    con.execute(
        f"CREATE TABLE eval_runs AS SELECT * FROM runs WHERE CAST(depart_ist AS DATE) = DATE '{iso}'"
    )
    con.execute(
        f"CREATE TABLE train_runs AS SELECT * FROM runs WHERE CAST(depart_ist AS DATE) <> DATE '{iso}'"
    )

    _build_profiles(con, "train_runs")

    con.execute(f"ATTACH '{config.REFERENCE_DB.as_posix()}' AS ref (READ_ONLY)")
    con.execute(
        """
        CREATE TABLE schedule AS
        SELECT from_station, to_station, scheduled_s FROM ref.station_pair_schedule
        """
    )
    con.execute("DETACH ref")

    for tbl in ("profile_pair_hour", "profile_pair_band", "profile_pair",
                "profile_global_band", "profile_global", "schedule",
                "train_runs", "eval_runs"):
        c = con.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
        print(f"{tbl:22s} {c:>10,}")

    con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", nargs="+", required=True)
    ap.add_argument("--out", default=str(config.TRAFFIC_DB))
    args = ap.parse_args()
    build(args.dates, Path(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
