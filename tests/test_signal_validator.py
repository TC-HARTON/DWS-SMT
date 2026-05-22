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
