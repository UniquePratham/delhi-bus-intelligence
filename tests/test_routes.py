"""Tests for route identifier normalisation."""
from __future__ import annotations

import pytest

from dbi.routes import route_key, route_keys, split_direction


class TestSplitDirection:
    @pytest.mark.parametrize(
        "route,base,direction",
        [
            ("874EUP", "874E", "UP"),
            ("874DOWN", "874", "DOWN"),
            ("D-034UP", "D-034", "UP"),
            ("801-IGI-INDDOWN", "801-IGI-IND", "DOWN"),
            ("OMS(+)BURARI-DHAULAKUAN", "OMS(+)BURARI-DHAULAKUAN", "NA"),
            ("", "", "NA"),
        ],
    )
    def test_split(self, route, base, direction):
        assert split_direction(route) == (base, direction)

    def test_lowercase_input(self):
        assert split_direction("874eup") == ("874E", "UP")


class TestRouteKey:
    @pytest.mark.parametrize(
        "route,key",
        [
            ("874EUP", "874UP"),
            ("874DOWN", "874DOWN"),
            ("D-034UP", "34UP"),
            ("D-0001DOWN", "1DOWN"),
            ("801-IGI-INDDOWN", "801DOWN"),
            ("130AUP", "130UP"),
            ("0118EXT(NS)DOWN", "118DOWN"),
        ],
    )
    def test_key(self, route, key):
        assert route_key(route) == key

    def test_no_numeric_route_returns_none(self):
        assert route_key("OMS(+)BURARI-DHAULAKUAN") is None

    def test_no_direction_returns_none(self):
        assert route_key("874") is None


class TestRouteKeys:
    def test_most_specific_first(self):
        keys = route_keys("874EUP")
        assert keys[0] == "874EUP"
        assert "874UP" in keys

    def test_no_duplicates(self):
        keys = route_keys("874UP")
        assert len(keys) == len(set(keys))

    def test_empty_for_unparseable(self):
        assert route_keys("OMS(+)BURARI-DHAULAKUAN") == []
