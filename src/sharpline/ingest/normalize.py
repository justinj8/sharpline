"""Player-name normalization used to match names across providers."""

from __future__ import annotations

import re
import unicodedata

_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})
_DROPPED = re.compile("['" + chr(0x2019) + ".]")
_NON_WORD = re.compile(r"[^a-z0-9\s]")


def normalize_name(name: str) -> str:
    """Lowercase, strip accents/punctuation/generational suffixes, collapse whitespace."""
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c)).lower()
    stripped = _DROPPED.sub("", stripped)
    stripped = _NON_WORD.sub(" ", stripped)
    return " ".join(t for t in stripped.split() if t not in _SUFFIXES)
