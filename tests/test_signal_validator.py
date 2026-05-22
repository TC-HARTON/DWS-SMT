"""Tests for the signal-validation layer."""

from __future__ import annotations

import math

import numpy as np
import pytest

from analyzer import signal_validator as sv


# --------------------------------------------------------------- Wilson interval
def test_wilson_interval_known_value():
    # 60 wins / 100 trials, z=1.96 → Wilson ≈ (0.5020, 0.6906).
    low, high = sv.wilson_interval(60, 100, z=1.96)
    assert low == pytest.approx(0.5020, abs=1e-3)
    assert high == pytest.approx(0.6906, abs=1e-3)
    assert low < high


def test_wilson_interval_zero_trials():
    # No data → the whole [0, 1] band, never a divide-by-zero.
    low, high = sv.wilson_interval(0, 0, z=1.96)
    assert low == 0.0
    assert high == 1.0


def test_wilson_interval_all_wins():
    low, high = sv.wilson_interval(20, 20, z=1.96)
    assert high == pytest.approx(1.0, abs=1e-9)
    assert 0.0 < low < 1.0


# ----------------------------------------------------------------- drawdown
def test_max_drawdown_basic():
    # equity curve: +10, +20, +5(DD 15), +25 → worst peak-to-trough = 15.
    assert sv.max_drawdown([10.0, 10.0, -15.0, 20.0]) == pytest.approx(15.0)


def test_max_drawdown_all_up():
    assert sv.max_drawdown([5.0, 5.0, 5.0]) == pytest.approx(0.0)


def test_max_drawdown_empty():
    assert sv.max_drawdown([]) == pytest.approx(0.0)


# --------------------------------------------------------------- summarize
def test_summarize_pnls_mixed():
    s = sv.summarize_pnls([100.0, -50.0, 100.0, -50.0])
    assert s["n"] == 4
    assert s["win_rate"] == pytest.approx(0.5)
    assert s["profit_factor"] == pytest.approx(2.0)        # 200 / 100
    assert s["expectancy"] == pytest.approx(25.0)          # 100 / 4


def test_summarize_pnls_no_losses():
    s = sv.summarize_pnls([10.0, 20.0])
    assert s["profit_factor"] == math.inf


def test_summarize_pnls_empty():
    s = sv.summarize_pnls([])
    assert s["n"] == 0
    assert s["win_rate"] == 0.0
    assert s["expectancy"] == 0.0
    assert s["profit_factor"] == 0.0
