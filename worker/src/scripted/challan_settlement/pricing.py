"""Offence → keyword price table.

Python port of OFFENCE_KEYWORD_PRICES / priceForOffence in
api/src/tasks/challanSettlement/prompt.ts. Used to derive `originalAmount`
when the Virtual Courts record's own Fine column is missing/zero.

KEEP IN SYNC with prompt.ts.
"""

from __future__ import annotations

# (keyword, price) — first substring match wins, same order as the TS array.
OFFENCE_KEYWORD_PRICES: list[tuple[str, int]] = [
    ("red light", 5000),
    ("jumping", 5000),
    ("permit", 10000),
    ("parking", 500),
    ("overspeed", 2000),
    ("over speed", 2000),
]


def price_for_offence(offence: str | None) -> int | None:
    """Return the keyword price for an offence string, or None if no keyword
    matches. Mirrors priceForOffence()."""
    if not offence:
        return None
    lower = offence.lower()
    for keyword, price in OFFENCE_KEYWORD_PRICES:
        if keyword in lower:
            return price
    return None
