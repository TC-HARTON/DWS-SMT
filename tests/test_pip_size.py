"""Tests for pip_size_for — the spread-display pip convention.

The bug this guards against: the naive even/odd-digit rule renders gold spreads
10x too large because gold's pip is $0.10 in DOLLARS regardless of whether the
broker quotes 2 digits (point 0.01) or 3 digits (point 0.001).
"""

from __future__ import annotations

import pytest

from analyzer.mt5_connector import pip_size_for


@pytest.mark.parametrize(
    "base,digits,point,expected",
    [
        # Gold: pip = $0.10 regardless of digit precision (the fix).
        ("XAUUSD", 2, 0.01, 0.10),
        ("XAUUSD", 3, 0.001, 0.10),
        ("XAUUSD.r", 2, 0.01, 0.10),       # broker suffix still detected
        # FX 5-digit (pipette): 1 pip = 10 * point.
        ("EURUSD", 5, 0.00001, 0.0001),
        ("GBPUSD", 5, 0.00001, 0.0001),
        ("AUDUSD", 5, 0.00001, 0.0001),
        # JPY 3-digit (pipette): 1 pip = 10 * point = 0.01.
        ("USDJPY", 3, 0.001, 0.01),
        ("EURJPY", 3, 0.001, 0.01),
        # Even-digit legacy FX: the point IS the pip.
        ("EURUSD", 4, 0.0001, 0.0001),
    ],
)
def test_pip_size_for(base, digits, point, expected):
    assert pip_size_for(base, digits, point) == pytest.approx(expected)


def test_gold_spread_is_one_pip_for_ten_cents():
    """A $0.10 gold spread must read as 1.0 pip (not 10) — the user's
    convention: $1.00 = 10 pips, $0.10 = 1 pip."""
    pip = pip_size_for("XAUUSD", 2, 0.01)
    assert (0.10 / pip) == pytest.approx(1.0)
    assert (1.00 / pip) == pytest.approx(10.0)
    assert (100.0 / pip) == pytest.approx(1000.0)
