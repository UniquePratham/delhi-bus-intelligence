"""Tests for the API ranking logic.

These exercise the scoring function directly, so they run without a server.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from dbi.api.main import _crowding_summary, _score
from dbi.journey.planner import Journey, Leg

T0 = datetime(2026, 10, 15, 18, 0)


def make_journey(minutes: float, categories: list[str]) -> Journey:
    """Build a journey with one bus leg per crowding category."""
    legs = []
    t = T0
    for i, cat in enumerate(categories):
        arrive = t + timedelta(minutes=minutes / len(categories))
        legs.append(
            Leg(
                kind="bus",
                from_station=i,
                to_station=i + 1,
                from_name=f"S{i}",
                to_name=f"S{i + 1}",
                depart=t,
                arrive=arrive,
                route_id=100 + i,
                route_name=f"R{i}",
                level="pair_hour",
                crowding={
                    "category": cat,
                    "confidence": "medium",
                    "target_type": "load_index",
                },
            )
        )
        t = arrive
    return Journey(legs=legs)


class TestScore:
    def test_fastest_ignores_crowding(self):
        fast_crowded = make_journey(30, ["crowded"])
        slow_empty = make_journey(60, ["seats_available"])
        assert _score(fast_crowded, "fastest", 0.35) < _score(slow_empty, "fastest", 0.35)

    def test_less_crowded_prefers_comfort(self):
        fast_crowded = make_journey(30, ["crowded"])
        slow_empty = make_journey(45, ["seats_available"])
        assert _score(slow_empty, "less_crowded", 0.35) < _score(
            fast_crowded, "less_crowded", 0.35
        )

    def test_less_crowded_rejects_an_absurd_detour(self):
        """Comfort must not justify an arbitrarily long trip."""
        fast_crowded = make_journey(30, ["crowded"])
        very_slow_empty = make_journey(95, ["seats_available"])
        assert _score(fast_crowded, "less_crowded", 0.35) < _score(
            very_slow_empty, "less_crowded", 0.35
        )

    def test_worst_leg_drives_the_penalty(self):
        """One crowded stretch must not be diluted by two empty ones."""
        partly_crowded = make_journey(40, ["seats_available", "crowded", "seats_available"])
        all_standing = make_journey(40, ["standing_room", "standing_room", "standing_room"])
        assert _score(all_standing, "less_crowded", 0.35) < _score(
            partly_crowded, "less_crowded", 0.35
        )

    def test_balanced_sits_between(self):
        fast_crowded = make_journey(30, ["crowded"])
        # Balanced must not simply mirror fastest.
        assert _score(fast_crowded, "balanced", 0.35) != _score(
            fast_crowded, "fastest", 0.35
        )

    def test_unknown_is_penalised_but_less_than_crowded(self):
        """Absence of evidence must not look like comfort, nor like crowding."""
        unknown = make_journey(40, ["unknown"])
        crowded = make_journey(40, ["crowded"])
        empty = make_journey(40, ["seats_available"])
        s_unknown = _score(unknown, "balanced", 0.35)
        s_crowded = _score(crowded, "balanced", 0.35)
        s_empty = _score(empty, "balanced", 0.35)
        assert s_empty < s_unknown < s_crowded

    def test_weight_scales_penalty(self):
        j = make_journey(40, ["crowded"])
        low = _score(j, "balanced", 0.1)
        high = _score(j, "balanced", 0.9)
        assert high > low

    def test_no_crowding_data_falls_back_to_time(self):
        j = make_journey(40, ["seats_available"])
        for leg in j.legs:
            leg.crowding = None
        assert _score(j, "balanced", 0.35) == pytest.approx(40.0)


class TestCrowdingSummary:
    def test_reports_worst_leg_not_average(self):
        """A crowded stretch must not be hidden by an empty one."""
        j = make_journey(40, ["seats_available", "crowded", "seats_available"])
        s = _crowding_summary(j)
        assert s["category"] == "crowded"

    def test_counts_known_legs(self):
        j = make_journey(40, ["seats_available", "unknown", "crowded"])
        s = _crowding_summary(j)
        assert s["legs_known"] == 2
        assert s["legs_total"] == 3

    def test_all_unknown_reports_unknown(self):
        j = make_journey(40, ["unknown", "unknown"])
        s = _crowding_summary(j)
        assert s["category"] == "unknown"
        assert s["legs_known"] == 0

    def test_target_type_is_always_load_index(self):
        j = make_journey(40, ["crowded"])
        assert _crowding_summary(j)["target_type"] == "load_index"

    def test_no_bus_legs_reports_unknown(self):
        j = Journey(legs=[])
        s = _crowding_summary(j)
        assert s["category"] == "unknown"
        assert s["confidence"] == "none"
