"""Shared duration formatting for the trtllm-main-failures-analysis scripts.

Rule: every time difference between commits, builds or runs that reaches a
report is written as "x days x hours x minutes". Numeric hours may be kept
alongside for statistics, never instead.
"""
from __future__ import annotations

import datetime as dt


def parse_iso(s: str) -> dt.datetime:
    """Parse GitHub / ci_report ISO-8601 timestamps ('Z' or offset) to an aware datetime."""
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def hours_between(a: str | dt.datetime | None, b: str | dt.datetime | None) -> float | None:
    """Signed hours from a to b (positive when b is later). None if either is missing."""
    if a is None or b is None:
        return None
    ta = parse_iso(a) if isinstance(a, str) else a
    tb = parse_iso(b) if isinstance(b, str) else b
    return round((tb - ta).total_seconds() / 3600, 1)


def human(hours: float | None) -> str | None:
    """Format a duration given in hours as 'x days x hours x minutes' (sign kept when negative)."""
    if hours is None:
        return None
    sign = "-" if hours < 0 else ""
    total_min = int(round(abs(hours) * 60))
    d, rem = divmod(total_min, 24 * 60)
    h, m = divmod(rem, 60)
    return f"{sign}{d} days {h} hours {m} minutes"


def duration_between(a, b) -> str | None:
    """Convenience: human(hours_between(a, b))."""
    return human(hours_between(a, b))
