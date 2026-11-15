"""Integration tests for the journey planner.

These require the built databases. They are skipped when the data has not been
prepared, so a fresh clone can still run the unit tests.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from dbi import config

pytestmark = pytest.mark.skipif(
    not (config.TRAFFIC_DB.exists() and config.REFERENCE_DB.exists()),
    reason="run the pipeline first: python -m dbi.reference && python -m dbi.traffic.model",
)


@pytest.fixture(scope="module")
def planner():
    from dbi.journey.planner import Planner

    return Planner()


class TestTravelTime:
    def test_returns_positive_seconds(self, planner):
        frm, to = next(iter(planner.pair))
        tt, level = planner.travel_time(frm, to, datetime(2026, 10, 15, 9, 0))
        assert tt > 0
        assert level in {
            "pair_hour", "pair_band", "pair", "schedule_scaled", "global_band", "global",
        }

    def test_peak_is_slower_than_night(self, planner):
        """The whole point of the model: the same stretch differs by hour."""
        # hour_of_week = weekday * 24 + hour. The five sampled days are all
        # Thursday or Friday, so only those two weekdays have profiles.
        peak_hours = {d * 24 + 9 for d in (3, 4)}
        night_hours = {d * 24 + 2 for d in (3, 4)}
        candidates = [
            (f, t)
            for (f, t, how) in planner.pair_hour
            if how in peak_hours
            and any((f, t, nh) in planner.pair_hour for nh in night_hours)
        ]
        assert candidates, "no pair observed at both 09:00 and 02:00"
        slower = 0
        sample = candidates[:200]
        for frm, to in sample:
            peak, _ = planner.travel_time(frm, to, datetime(2026, 10, 15, 9, 0))
            night, _ = planner.travel_time(frm, to, datetime(2026, 10, 15, 2, 0))
            if peak > night:
                slower += 1
        assert slower > len(sample) * 0.6

    def test_unknown_pair_falls_back(self, planner):
        tt, level = planner.travel_time(-1, -2, datetime(2026, 10, 15, 9, 0))
        assert tt > 0
        assert level in {"global_band", "global"}


class TestResolvePlace:
    def test_resolves_known_name(self, planner):
        lat, lon, name = planner.resolve_place(name="Nehru Place")
        assert 28.4 < lat < 28.8
        assert 76.9 < lon < 77.5
        assert "nehru place" in name.lower()

    def test_prefers_exact_over_substring(self, planner):
        _, _, name = planner.resolve_place(name="Nehru Place")
        assert name.lower() == "nehru place"

    def test_accepts_coordinates(self, planner):
        lat, lon, label = planner.resolve_place(lat=28.61, lon=77.20)
        assert (lat, lon) == (28.61, 77.20)

    def test_raises_on_unknown_name(self, planner):
        with pytest.raises(ValueError):
            planner.resolve_place(name="zzzz not a place zzzz")


class TestPlan:
    def test_returns_journeys(self, planner):
        o = planner.resolve_place(name="Kashmere Gate ISBT")
        d = planner.resolve_place(name="Nehru Place")
        js = planner.plan((o[0], o[1]), (d[0], d[1]), datetime(2026, 10, 15, 8, 30))
        assert js, "expected at least one journey"

    def test_journeys_are_time_ordered(self, planner):
        o = planner.resolve_place(name="Kashmere Gate ISBT")
        d = planner.resolve_place(name="Nehru Place")
        js = planner.plan((o[0], o[1]), (d[0], d[1]), datetime(2026, 10, 15, 8, 30))
        durations = [j.duration_s for j in js]
        assert durations == sorted(durations)

    def test_legs_are_contiguous_in_time(self, planner):
        o = planner.resolve_place(name="Kashmere Gate ISBT")
        d = planner.resolve_place(name="Nehru Place")
        js = planner.plan((o[0], o[1]), (d[0], d[1]), datetime(2026, 10, 15, 8, 30))
        for j in js:
            for a, b in zip(j.legs, j.legs[1:], strict=False):
                assert a.arrive <= b.depart

    def test_transfers_within_limit(self, planner):
        o = planner.resolve_place(name="Kashmere Gate ISBT")
        d = planner.resolve_place(name="Nehru Place")
        for limit in (0, 1, 2):
            js = planner.plan(
                (o[0], o[1]), (d[0], d[1]), datetime(2026, 10, 15, 8, 30),
                max_transfers=limit,
            )
            for j in js:
                assert j.transfers <= limit

    def test_bus_legs_are_merged_per_route(self, planner):
        """A ride on one route must not be reported as many transfers."""
        o = planner.resolve_place(name="Kashmere Gate ISBT")
        d = planner.resolve_place(name="Nehru Place")
        js = planner.plan((o[0], o[1]), (d[0], d[1]), datetime(2026, 10, 15, 8, 30))
        for j in js:
            routes = [leg.route_id for leg in j.bus_legs]
            assert len(routes) == len(set(routes)), "same route appears twice in one journey"

    def test_different_hours_give_different_answers(self, planner):
        o = planner.resolve_place(name="Kashmere Gate ISBT")
        d = planner.resolve_place(name="Nehru Place")
        peak = planner.plan((o[0], o[1]), (d[0], d[1]), datetime(2026, 10, 15, 9, 0))
        night = planner.plan((o[0], o[1]), (d[0], d[1]), datetime(2026, 10, 15, 23, 0))
        assert peak and night
        assert peak[0].duration_s != night[0].duration_s

    def test_no_route_returns_empty(self, planner):
        # Two points far outside Delhi.
        js = planner.plan((0.0, 0.0), (1.0, 1.0), datetime(2026, 10, 15, 9, 0))
        assert js == []


class TestCrowding:
    def test_enrichment_marks_unknown_explicitly(self, planner):
        o = planner.resolve_place(name="Kashmere Gate ISBT")
        d = planner.resolve_place(name="Nehru Place")
        js = planner.plan((o[0], o[1]), (d[0], d[1]), datetime(2026, 10, 15, 8, 30))
        for j in js:
            planner.enrich_crowding(j)
            for leg in j.bus_legs:
                assert leg.crowding is not None
                assert leg.crowding["category"] in {
                    "seats_available", "standing_room", "crowded", "unknown",
                }
                assert leg.crowding["target_type"] == "load_index"

    def test_unknown_is_never_reported_as_low(self, planner):
        """A missing estimate must not be silently treated as comfortable."""
        o = planner.resolve_place(name="Kashmere Gate ISBT")
        d = planner.resolve_place(name="Nehru Place")
        js = planner.plan((o[0], o[1]), (d[0], d[1]), datetime(2026, 10, 15, 8, 30))
        for j in js:
            planner.enrich_crowding(j)
            for leg in j.bus_legs:
                if leg.crowding["category"] == "unknown":
                    assert leg.crowding["confidence"] == "none"


class TestDegradation:
    def test_planner_works_without_crowding_db(self):
        """The brief requires the planner to survive a missing crowding module."""
        from pathlib import Path

        from dbi.journey.planner import Planner

        p = Planner(crowding_db=Path("data/processed/does_not_exist.duckdb"))
        o = p.resolve_place(name="Kashmere Gate ISBT")
        d = p.resolve_place(name="Nehru Place")
        js = p.plan((o[0], o[1]), (d[0], d[1]), datetime(2026, 10, 15, 8, 30))
        assert js, "planner must still return journeys without crowding data"
        for j in js:
            p.enrich_crowding(j)
            for leg in j.bus_legs:
                assert leg.crowding is None or leg.crowding["category"] == "unknown"
