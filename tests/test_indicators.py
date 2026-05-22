"""Verify SPEC §6 indicators against hand-computed reference values.

These reference values were derived from Wilder's original formulas
exactly (no library dependency); a mismatch here is a real bug, not a
floating-point quirk.
"""

from __future__ import annotations

import numpy as np
import pytest

from analyzer.indicators import adx, atr, ema, last_finite, rsi, true_range


# ----------------------------------------------------------------- EMA tests

def test_ema_seed_is_sma_of_first_period():
    close = np.arange(1, 11, dtype=float)        # 1..10
    out = ema(close, period=5)
    # first valid value is at index 4 (period-1), equals SMA(1..5) = 3.0
    assert np.isnan(out[:4]).all()
    assert out[4] == pytest.approx(3.0, abs=1e-12)

def test_ema_recurrence_matches_reference():
    close = np.array([10, 11, 12, 13, 14, 15, 16, 17], dtype=float)
    out = ema(close, period=3)
    # alpha = 2/(3+1) = 0.5, seed = SMA(10,11,12) = 11
    # y[3] = 0.5*13 + 0.5*11 = 12
    # y[4] = 0.5*14 + 0.5*12 = 13
    # y[5] = 0.5*15 + 0.5*13 = 14
    expected = [np.nan, np.nan, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0]
    np.testing.assert_allclose(out, expected, rtol=1e-12, equal_nan=True)

def test_ema_validates_period():
    with pytest.raises(ValueError):
        ema(np.array([1.0, 2.0]), period=0)

def test_ema_returns_all_nan_for_short_input():
    out = ema(np.array([1.0, 2.0]), period=5)
    assert np.isnan(out).all()


# ----------------------------------------------------------------- TR / ATR

def test_true_range_first_bar_is_hl():
    h = np.array([10.0, 11.0, 12.0])
    l = np.array([9.0, 10.0, 11.0])
    c = np.array([9.5, 10.5, 11.5])
    tr = true_range(h, l, c)
    assert tr[0] == pytest.approx(1.0)              # H-L for first bar
    # bar 1: H-L=1, |H-prevC|=|11-9.5|=1.5, |L-prevC|=|10-9.5|=0.5 → 1.5
    assert tr[1] == pytest.approx(1.5)
    # bar 2: H-L=1, |12-10.5|=1.5, |11-10.5|=0.5 → 1.5
    assert tr[2] == pytest.approx(1.5)

def test_atr_warmup_then_wilder():
    h = np.array([10, 11, 12, 13, 14], dtype=float)
    l = np.array([ 9, 10, 11, 12, 13], dtype=float)
    c = np.array([ 9.5, 10.5, 11.5, 12.5, 13.5], dtype=float)
    # TR = [1.0, 1.5, 1.5, 1.5, 1.5]
    out = atr(h, l, c, period=3)
    # ATR[2] = mean(TR[0..2]) = (1.0 + 1.5 + 1.5) / 3 = 1.333...
    assert np.isnan(out[:2]).all()
    assert out[2] == pytest.approx(4.0 / 3.0, rel=1e-12)
    # ATR[3] = (1.333.. * 2 + 1.5) / 3 = 1.388...
    assert out[3] == pytest.approx((4.0 / 3.0 * 2 + 1.5) / 3.0, rel=1e-12)


# ----------------------------------------------------------------- RSI

def test_rsi_constant_increase_is_100():
    close = np.arange(1, 30, dtype=float)
    out = rsi(close, period=14)
    # All diffs positive ⇒ avg_loss = 0 ⇒ RSI = 100 by convention
    last = last_finite(out)
    assert last == pytest.approx(100.0)

def test_rsi_constant_decrease_is_zero():
    close = np.arange(30, 1, -1, dtype=float)
    out = rsi(close, period=14)
    last = last_finite(out)
    assert last == pytest.approx(0.0)

def test_rsi_alternating_around_50():
    np.random.seed(0)
    close = 100 + np.cumsum(np.random.normal(0, 1, size=200))
    out = rsi(close, period=14)
    finite = out[np.isfinite(out)]
    assert 0.0 <= finite.min() <= 100.0
    assert 0.0 <= finite.max() <= 100.0
    assert 30.0 < np.mean(finite) < 70.0   # roughly mean-reverting series


# ----------------------------------------------------------------- ADX

def test_adx_zero_for_flat_market():
    # Identical highs/lows/closes ⇒ no directional movement, no true range
    close = np.full(50, 100.0)
    a, dip, dim = adx(close, close, close, period=14)
    # All TR=0 → all DI undefined → DX defaults to 0 in implementation
    last_adx = last_finite(a)
    assert last_adx == pytest.approx(0.0)

def test_adx_high_for_strong_trend():
    n = 80
    close = np.linspace(100.0, 150.0, n)
    high = close + 0.1
    low = close - 0.1
    a, dip, dim = adx(high, low, close, period=14)
    last_adx = last_finite(a)
    last_dip = last_finite(dip)
    last_dim = last_finite(dim)
    # Trend up ⇒ ADX should be well above the trend threshold,
    # +DI should dominate -DI.
    assert last_adx > 80.0
    assert last_dip > last_dim


# ----------------------------------------------------------------- last_finite

def test_last_finite_returns_last_finite_value():
    a = np.array([1.0, np.nan, 3.0, np.nan])
    assert last_finite(a) == 3.0

def test_last_finite_returns_none_when_all_nan():
    a = np.array([np.nan, np.nan])
    assert last_finite(a) is None

def test_last_finite_returns_none_for_empty():
    a = np.array([], dtype=float)
    assert last_finite(a) is None
