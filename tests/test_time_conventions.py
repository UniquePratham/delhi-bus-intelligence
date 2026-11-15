"""Tests for time conventions shared between SQL and Python.

The weekday convention is the kind of bug that produces plausible-looking
output while being wrong by one day, so it is pinned here.
"""
from __future__ import annotations

from datetime import datetime

import duckdb
import pytest

from dbi.traffic.model import HOUR_BANDS, _band_case


def sql_weekday(date_str: str) -> int:
    """The conversion used by the pipelines: DuckDB dow -> 0=Monday."""
    con = duckdb.connect()
    return con.execute(
        f"SELECT CAST((CAST(extract(dow FROM DATE '{date_str}') AS INTEGER) + 6) % 7 AS INTEGER)"
    ).fetchone()[0]


class TestWeekdayConvention:
    @pytest.mark.parametrize(
        "date_str,expected",
        [
            ("2026-10-05", 0),  # Monday
            ("2026-10-06", 1),  # Tuesday
            ("2026-10-07", 2),  # Wednesday
            ("2026-10-08", 3),  # Thursday
            ("2026-10-09", 4),  # Friday
            ("2026-10-10", 5),  # Saturday
            ("2026-10-11", 6),  # Sunday
        ],
    )
    def test_sql_matches_python_weekday(self, date_str, expected):
        assert sql_weekday(date_str) == expected
        assert datetime.fromisoformat(date_str).weekday() == expected

    def test_duckdb_dow_is_sunday_based(self):
        """Documents the trap: DuckDB's dow is 0=Sunday, not 0=Monday."""
        con = duckdb.connect()
        assert con.execute("SELECT extract(dow FROM DATE '2026-10-11')").fetchone()[0] == 0
        assert con.execute("SELECT extract(dow FROM DATE '2026-10-05')").fetchone()[0] == 1

    def test_hour_of_week_fits_smallint(self):
        """0..167 needs SMALLINT; TINYINT overflows at 127."""
        assert 6 * 24 + 23 == 167
        assert 167 > 127


class TestHourBands:
    def test_every_hour_maps_to_exactly_one_band(self):
        for hour in range(24):
            matches = [name for lo, hi, name in HOUR_BANDS if lo <= hour <= hi]
            assert len(matches) == 1, f"hour {hour} matched {matches}"

    def test_bands_cover_all_24_hours(self):
        covered = set()
        for lo, hi, _name in HOUR_BANDS:
            covered.update(range(lo, hi + 1))
        assert covered == set(range(24))

    def test_band_case_sql_is_well_formed(self):
        sql = _band_case("hour")
        assert sql.startswith("CASE")
        assert sql.endswith("END")
        for _lo, _hi, name in HOUR_BANDS:
            assert f"'{name}'" in sql
