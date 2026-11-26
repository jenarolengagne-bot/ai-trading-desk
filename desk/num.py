"""Finite numbers only. Missing, NaN and infinity are unknown, never zero."""
from __future__ import annotations

import math


def finite_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def finite_positive(value) -> float | None:
    number = finite_number(value)
    if number is None or number <= 0:
        return None
    return number


def quote_price(quote) -> float | None:
    if not isinstance(quote, dict):
        return None
    return finite_positive(quote.get("price_usd"))


def quote_volume(quote) -> tuple[float, float] | None:
    """Both windows present and finite. Missing is unknown, not a zero print."""
    if not isinstance(quote, dict):
        return None
    if quote.get("volume_h6") is None or quote.get("volume_h24") is None:
        return None
    h6 = finite_number(quote.get("volume_h6"))
    h24 = finite_number(quote.get("volume_h24"))
    if h6 is None or h24 is None or h6 < 0 or h24 <= 0:
        return None
    return h6, h24
