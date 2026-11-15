"""Run the whole pipeline end to end.

Each stage is idempotent, so re-running rebuilds from the raw data. Stages are
ordered by dependency; the GPS stages are the slow ones.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time

from dbi import config

# Days to process. The last one is the evaluation holdout.
#
# These cover a full week (Mon 5 Oct – Sun 11 Oct) plus three later Thursdays,
# so every weekday and both weekend days have a profile. An earlier version
# used five Thursdays, which meant Monday–Wednesday and weekends had no
# hour-of-week evidence at all.
DEFAULT_DATES = [
    "2026_10_01",  # Thu
    "2026_10_05",  # Mon
    "2026_10_06",  # Tue
    "2026_10_07",  # Wed
    "2026_10_08",  # Thu
    "2026_10_09",  # Fri
    "2026_10_10",  # Sat
    "2026_10_11",  # Sun
    "2026_10_15",  # Thu
    "2026_10_22",  # Thu
    "2026_10_29",  # Thu (holdout)
]


def run(module: str, *args: str) -> None:
    cmd = [sys.executable, "-m", module, *args]
    print(f"\n$ {' '.join(cmd)}", flush=True)
    t = time.time()
    r = subprocess.run(cmd, env={**__import__("os").environ, "PYTHONPATH": "src"})
    if r.returncode != 0:
        raise SystemExit(f"{module} failed with {r.returncode}")
    print(f"  done in {time.time() - t:.1f}s", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", nargs="+", default=DEFAULT_DATES)
    ap.add_argument("--skip-gps", action="store_true", help="reuse existing GPS parquet")
    ap.add_argument("--skip-eval", action="store_true")
    args = ap.parse_args()

    run("dbi.reference")
    run("dbi.stations")
    run("dbi.traffic.schedule")

    if not args.skip_gps:
        for d in args.dates:
            run("dbi.traffic.ingest", "--date", d)
            run("dbi.traffic.stop_passage", "--date", d)

    run("dbi.traffic.model", "--dates", *args.dates)
    run("dbi.crowding.ingest")
    run("dbi.crowding.model")

    if not args.skip_eval:
        run("dbi.traffic.evaluate")
        run("dbi.traffic.temporal")
        run("dbi.crowding.evaluate")
        run("dbi.journey.validate", "--sample", "400")

    print("\nPipeline complete.")
    print(f"  reference : {config.REFERENCE_DB}")
    print(f"  traffic   : {config.TRAFFIC_DB}")
    print(f"  crowding  : {config.CROWDING_DB}")
    print(f"  artifacts : {config.ARTIFACTS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
