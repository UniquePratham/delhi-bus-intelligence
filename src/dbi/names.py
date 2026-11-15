"""Normalise stop names so ticket records can be linked to GTFS stations.

Ticket stop names and GTFS stop names are written by different systems, so
they differ in punctuation, spacing, abbreviations and suffixes:

    "Badarpur Border(T)"  vs  "Badarpur Border"
    "Aiims"               vs  "AIIMS"
    "Rang Puri (Nh8)"     vs  "Rangpuri"

A normalised key is built by lowercasing, stripping punctuation and bracketed
qualifiers, expanding a small set of known abbreviations, and collapsing
whitespace. This is deliberately conservative: it never merges two names that
differ in their significant tokens.
"""
from __future__ import annotations

import re

# Abbreviations that appear in one source but not the other.
ABBREV = {
    "isbt": "isbt",
    "stn": "station",
    "sta": "station",
    "hosp": "hospital",
    "univ": "university",
    "col": "college",
    "sch": "school",
    "mkt": "market",
    "rd": "road",
    "nagar": "nagar",
    "extn": "extension",
    "ext": "extension",
    "sec": "sector",
    "blk": "block",
    "twr": "tower",
    "flr": "floor",
    "bdr": "border",
    "xing": "crossing",
    "crssing": "crossing",
    "chowk": "chowk",
    "chk": "chowk",
    "t": "terminal",
    "term": "terminal",
    "depot": "depot",
    "dpt": "depot",
}

# Tokens that carry no distinguishing information.
STOPWORDS = {"the", "of", "and", "near", "opp", "opposite", "nr", "at"}


def normalise(name: str) -> str:
    """Return a comparison key for a stop name."""
    if not name:
        return ""
    s = name.lower().strip()
    # Drop bracketed qualifiers: "(T)", "(NH8)", "(SDM Court)".
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"\[[^\]]*\]", " ", s)
    # Normalise separators.
    s = s.replace("/", " ").replace("-", " ").replace(".", " ").replace(",", " ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    tokens = []
    for tok in s.split():
        if tok in STOPWORDS:
            continue
        tokens.append(ABBREV.get(tok, tok))
    return " ".join(tokens)


def key_variants(name: str) -> list[str]:
    """Keys to try, most specific first.

    The full normalised key is tried first. Looser keys follow so that
    "Metro Station Kashmere Gate" can still reach "Kashmere Gate Metro Station"
    and "Nand Nagri (T.)" can reach "Nand Nagari Terminal".
    """
    base = normalise(name)
    out = [base]
    if not base:
        return out
    parts = base.split()
    # Drop a trailing qualifier: "nehru place terminal" -> "nehru place".
    if len(parts) > 2 and parts[-1] in {
        "terminal", "depot", "station", "crossing", "border", "market", "metro",
    }:
        out.append(" ".join(parts[:-1]))
    # Drop leading qualifiers: "metro station kashmere gate" -> "kashmere gate".
    qualifiers = {"metro", "station", "isbt", "terminal", "bus", "stop"}
    i = 0
    while i < len(parts) - 2 and parts[i] in qualifiers:
        i += 1
    if i:
        out.append(" ".join(parts[i:]))
    # Sorted tokens, so word order does not matter.
    out.append(" ".join(sorted(parts)))
    seen = set()
    uniq = []
    for k in out:
        if k and k not in seen:
            seen.add(k)
            uniq.append(k)
    return uniq


def token_set(name: str) -> frozenset[str]:
    """Significant tokens of a name, for subset matching."""
    return frozenset(normalise(name).split())


def is_subset_match(a: str, b: str) -> bool:
    """True when one name's tokens are contained in the other's.

    Requires at least two shared tokens so that a single generic word
    ("terminal", "depot") cannot link two unrelated places.
    """
    ta, tb = token_set(a), token_set(b)
    if not ta or not tb:
        return False
    small, large = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return len(small) >= 2 and small <= large
