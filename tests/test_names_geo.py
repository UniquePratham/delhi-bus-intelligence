"""Unit tests for the name normaliser and geospatial helpers."""
from __future__ import annotations

import pytest

from dbi.geo import haversine_m, point_segment_distance_m
from dbi.names import is_subset_match, key_variants, normalise


class TestNormalise:
    def test_lowercases_and_strips_punctuation(self):
        assert normalise("Badarpur Border(T)") == "badarpur border"

    def test_drops_bracketed_qualifiers(self):
        assert normalise("Rang Puri (Nh8)") == "rang puri"

    def test_collapses_separators(self):
        assert normalise("Ashok Nagar / Meet Nagar") == "ashok nagar meet nagar"

    def test_expands_known_abbreviations(self):
        assert normalise("Babar Pur Extn.") == "babar pur extension"

    def test_drops_stopwords(self):
        assert normalise("The Mall Near ISBT") == "mall isbt"

    def test_empty_input(self):
        assert normalise("") == ""

    def test_handles_none_like_input(self):
        assert normalise(None) == ""


class TestKeyVariants:
    def test_includes_base_key_first(self):
        assert key_variants("Nehru Place")[0] == "nehru place"

    def test_drops_trailing_qualifier(self):
        assert "nehru place" in key_variants("Nehru Place Terminal")

    def test_drops_leading_qualifier(self):
        assert "kashmere gate" in key_variants("Metro Station Kashmere Gate")

    def test_word_order_insensitive_variant(self):
        assert "gate kashmere" in key_variants("Kashmere Gate")

    def test_no_duplicates(self):
        v = key_variants("Nehru Place")
        assert len(v) == len(set(v))


class TestSubsetMatch:
    def test_matches_when_tokens_contained(self):
        assert is_subset_match("Metro Station Kashmere Gate", "Kashmere Gate Metro Station Gate 4")

    def test_rejects_single_generic_token(self):
        assert not is_subset_match("Terminal", "Nehru Place Terminal")

    def test_rejects_unrelated_names(self):
        assert not is_subset_match("Nehru Place", "Nehru Nagar")

    def test_rejects_empty(self):
        assert not is_subset_match("", "Nehru Place")


class TestHaversine:
    def test_zero_distance(self):
        assert haversine_m(28.6, 77.2, 28.6, 77.2) == pytest.approx(0.0, abs=1e-6)

    def test_known_delhi_distance(self):
        # Kashmere Gate ISBT to Nehru Place is roughly 13 km.
        d = haversine_m(28.6669, 77.2293, 28.5486, 77.2492)
        assert 12_000 < d < 14_500

    def test_symmetry(self):
        a = haversine_m(28.6, 77.2, 28.7, 77.3)
        b = haversine_m(28.7, 77.3, 28.6, 77.2)
        assert a == pytest.approx(b, rel=1e-9)


class TestPointSegment:
    def test_point_on_segment(self):
        d, t = point_segment_distance_m(28.60, 77.20, 28.60, 77.20, 28.61, 77.20)
        assert d == pytest.approx(0.0, abs=0.5)
        assert t == pytest.approx(0.0, abs=1e-6)

    def test_perpendicular_distance(self):
        # ~111 m north of a segment running east.
        d, t = point_segment_distance_m(28.601, 77.200, 28.600, 77.200, 28.600, 77.202)
        assert 100 < d < 125
        assert 0.0 <= t <= 1.0

    def test_clamps_beyond_endpoint(self):
        d, t = point_segment_distance_m(28.600, 77.210, 28.600, 77.200, 28.600, 77.202)
        assert t == pytest.approx(1.0)
        assert d > 700
