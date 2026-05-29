"""Tests for the recommended-lot ladder (analyzer.position_sizing)."""

from __future__ import annotations

import math

import pytest

import config
from analyzer.position_sizing import recommended_lot


@pytest.mark.parametrize(
    "equity,expected",
    [
        (100_000, 0.01),       # exactly one step
        (199_999, 0.01),       # just below two steps -> still 0.01
        (200_000, 0.02),
        (250_000, 0.02),       # floor, not round
        (1_000_000, 0.10),
        (5_000_000, 0.50),
        (50_000, 0.01),        # below one step -> min floor
        (0, 0.01),             # zero -> min
    ],
)
def test_ladder_steps(equity, expected):
    assert recommended_lot(equity) == pytest.approx(expected)


def test_caps_at_lot_max():
    # 100M equity would be 10.00 lots raw -> exactly the cap; 1B stays capped.
    assert recommended_lot(100_000_000) == pytest.approx(config.LOT_MAX)
    assert recommended_lot(1_000_000_000) == pytest.approx(config.LOT_MAX)


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), -1.0, -100_000.0])
def test_bad_equity_returns_min(bad):
    assert recommended_lot(bad) == pytest.approx(config.LOT_MIN)


def test_result_is_on_001_grid():
    for eq in (123_456, 777_777, 2_345_678, 9_999_999):
        lot = recommended_lot(eq)
        # lot is a clean multiple of 0.01 (no float dust like 0.030000001)
        assert math.isclose(round(lot * 100) / 100, lot, abs_tol=1e-9)
        assert config.LOT_MIN <= lot <= config.LOT_MAX
