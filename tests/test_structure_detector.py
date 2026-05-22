"""Unit tests for analyzer.structure_detector (SPEC §10)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from analyzer import structure_detector
from analyzer.structure_types import LevelKind, LevelSource


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _make_daily(n: int, start: float = 100.0) -> pd.DataFrame:
    """Synthetic D1/W1/MN1 dataframe with deterministic high/low for testing."""
    rng = np.random.default_rng(0)
    close = start + np.cumsum(rng.normal(0.0, 0.2, n))
    high = close + 0.5
    low = close - 0.5
    idx = pd.date_range("2026-01-01", periods=n, freq="1D", tz="UTC")
    return pd.DataFrame({
        "open": close, "high": high, "low": low, "close": close,
        "tick_volume": np.full(n, 100, dtype=np.int64),
        "spread": np.zeros(n, dtype=np.int32),
        "real_volume": np.zeros(n, dtype=np.int64),
    }, index=idx)


def _make_m1(n: int = 600) -> pd.DataFrame:
    """Synthetic M1 dataframe spanning ~10h ending now (UTC), needed for VWAP + sessions."""
    rng = np.random.default_rng(1)
    close = 100.0 + np.cumsum(rng.normal(0.0, 0.005, n))
    high = close + 0.01
    low = close - 0.01
    end = pd.Timestamp.now("UTC").floor("min")
    idx = pd.date_range(end=end, periods=n, freq="1min", tz="UTC")
    return pd.DataFrame({
        "open": close, "high": high, "low": low, "close": close,
        "tick_volume": np.full(n, 50, dtype=np.int64),
        "spread": np.zeros(n, dtype=np.int32),
        "real_volume": np.zeros(n, dtype=np.int64),
    }, index=idx)


# --------------------------------------------------------------------------- #
# PDH / PDL / PWH / PWL / PMH / PML
# --------------------------------------------------------------------------- #

def test_previous_period_levels_present_when_multiple_bars():
    d1 = _make_daily(5)
    w1 = _make_daily(3)
    mn1 = _make_daily(3)
    levels = structure_detector.detect_all(
        "XAUUSD", {"D1": d1, "W1": w1, "MN1": mn1, "M15": _make_daily(20)},
        current_price=100.0,
    )
    kinds = {lv.kind for lv in levels}
    assert LevelKind.PREV_DAY_HIGH in kinds
    assert LevelKind.PREV_DAY_LOW in kinds
    assert LevelKind.PREV_WEEK_HIGH in kinds
    assert LevelKind.PREV_WEEK_LOW in kinds
    assert LevelKind.PREV_MONTH_HIGH in kinds
    assert LevelKind.PREV_MONTH_LOW in kinds


def test_pdh_pdl_take_prev_bar_not_current():
    d1 = _make_daily(5)
    # Force the second-to-last bar to a known high/low.
    d1.iloc[-2, d1.columns.get_loc("high")] = 999.0
    d1.iloc[-2, d1.columns.get_loc("low")] = 1.0
    levels = structure_detector.detect_all(
        "XAUUSD", {"D1": d1}, current_price=100.0,
    )
    pdh = [lv for lv in levels if lv.kind == LevelKind.PREV_DAY_HIGH]
    pdl = [lv for lv in levels if lv.kind == LevelKind.PREV_DAY_LOW]
    assert len(pdh) == 1 and pdh[0].price == 999.0
    assert len(pdl) == 1 and pdl[0].price == 1.0


def test_previous_levels_omitted_when_only_one_bar():
    d1 = _make_daily(1)
    levels = structure_detector.detect_all(
        "XAUUSD", {"D1": d1}, current_price=100.0,
    )
    pdh = [lv for lv in levels if lv.kind == LevelKind.PREV_DAY_HIGH]
    assert not pdh


# --------------------------------------------------------------------------- #
# Round numbers
# --------------------------------------------------------------------------- #

def test_round_numbers_centred_on_current_price():
    levels = structure_detector.detect_all(
        "XAUUSD", {}, current_price=3017.0, round_step=50.0, round_rungs=2,
    )
    rounds = [lv.price for lv in levels if lv.kind == LevelKind.ROUND_NUMBER]
    # 3017 rounds to 3000 (closer than 3050); with rungs=2: 2900, 2950, 3000, 3050, 3100
    assert sorted(rounds) == [2900.0, 2950.0, 3000.0, 3050.0, 3100.0]


def test_round_numbers_use_config_step_when_not_overridden():
    levels = structure_detector.detect_all(
        "USDJPY", {}, current_price=158.7,
    )
    rounds = sorted(lv.price for lv in levels if lv.kind == LevelKind.ROUND_NUMBER)
    # USDJPY config step = 0.5; centre rounds to 158.5; rungs=3
    expected = [157.0, 157.5, 158.0, 158.5, 159.0, 159.5, 160.0]
    assert rounds == pytest.approx(expected, abs=1e-9)


def test_round_numbers_heuristic_fallback_for_unknown_symbol():
    levels = structure_detector.detect_all(
        "ABCDEF", {}, current_price=4500.0, round_rungs=1,
    )
    rounds = [lv.price for lv in levels if lv.kind == LevelKind.ROUND_NUMBER]
    # Heuristic for >= 1000 → step 50.
    assert sorted(rounds) == [4450.0, 4500.0, 4550.0]


# --------------------------------------------------------------------------- #
# Swing detection
# --------------------------------------------------------------------------- #

def test_swings_find_obvious_fractal_high():
    # Build M15 closes so bar 10 is a strict high vs bars 8, 9, 11, 12.
    n = 30
    high = np.full(n, 100.0)
    low = np.full(n, 99.0)
    high[10] = 110.0       # the swing-high
    df = pd.DataFrame({
        "open": np.full(n, 100.0),
        "high": high, "low": low, "close": np.full(n, 100.0),
        "tick_volume": np.full(n, 1, dtype=np.int64),
        "spread": np.zeros(n, dtype=np.int32),
        "real_volume": np.zeros(n, dtype=np.int64),
    }, index=pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC"))

    levels = structure_detector.detect_all(
        "X", {"M15": df}, current_price=100.0,
    )
    highs = [lv for lv in levels if lv.kind == LevelKind.SWING_HIGH]
    assert any(lv.price == 110.0 for lv in highs)


def test_swings_drop_recent_indeterminate_bars():
    # Construct an unambiguous high at index n-1 (latest bar). Because the
    # fractal needs `lookback` bars on each side, the latest `lookback` bars
    # cannot be classified yet.
    n = 30
    high = np.full(n, 100.0)
    high[-1] = 200.0
    df = pd.DataFrame({
        "open": np.full(n, 100.0),
        "high": high, "low": np.full(n, 99.0), "close": np.full(n, 100.0),
        "tick_volume": np.full(n, 1, dtype=np.int64),
        "spread": np.zeros(n, dtype=np.int32),
        "real_volume": np.zeros(n, dtype=np.int64),
    }, index=pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC"))

    levels = structure_detector.detect_all(
        "X", {"M15": df}, current_price=100.0,
    )
    highs = [lv for lv in levels if lv.kind == LevelKind.SWING_HIGH]
    assert not any(lv.price == 200.0 for lv in highs), \
        "the latest bar cannot have a completed fractal"


# --------------------------------------------------------------------------- #
# Session highs / lows
# --------------------------------------------------------------------------- #

def test_session_levels_emitted_when_m1_present():
    m1 = _make_m1(600)  # 10 hours back
    levels = structure_detector.detect_all(
        "X", {"M1": m1}, current_price=100.0,
    )
    session_kinds = [lv for lv in levels
                     if lv.kind in (LevelKind.SESSION_HIGH, LevelKind.SESSION_LOW)]
    # We span ~10h so at least one session should match.
    assert session_kinds
    for lv in session_kinds:
        assert lv.meta.get("session") in {"Asia", "Europe", "NY"}


# --------------------------------------------------------------------------- #
# VWAP
# --------------------------------------------------------------------------- #

def test_vwap_reflects_typical_price_weighted_by_volume():
    # Two bars today, one with 100 vol @ price 100, one with 100 vol @ price 110.
    today = pd.Timestamp.now("UTC").normalize()
    idx = pd.DatetimeIndex([today + pd.Timedelta(minutes=1),
                            today + pd.Timedelta(minutes=2)])
    df = pd.DataFrame({
        "open":  [100.0, 110.0],
        "high":  [100.0, 110.0],
        "low":   [100.0, 110.0],
        "close": [100.0, 110.0],
        "tick_volume": [100, 100],
        "spread": [0, 0],
        "real_volume": [0, 0],
    }, index=idx)
    # Typical = close in this no-wick case, so expected VWAP = (100+110)/2 = 105
    levels = structure_detector.detect_all(
        "X", {"M1": df}, current_price=105.0,
    )
    vwap = [lv for lv in levels if lv.kind == LevelKind.VWAP]
    assert len(vwap) == 1
    assert vwap[0].price == pytest.approx(105.0, abs=1e-9)
    assert vwap[0].source == LevelSource.AUTO_VWAP


# --------------------------------------------------------------------------- #
# Aggregated entry point
# --------------------------------------------------------------------------- #

def test_detect_all_handles_empty_rates_gracefully():
    levels = structure_detector.detect_all("X", {}, current_price=None)
    assert levels == []  # no inputs → no outputs


def test_detect_all_returns_only_supported_kinds():
    d1 = _make_daily(40)
    m15 = _make_daily(40)
    m1 = _make_m1(120)
    levels = structure_detector.detect_all(
        "XAUUSD", {"D1": d1, "W1": d1, "MN1": d1, "M15": m15, "M1": m1},
        current_price=100.0,
    )
    permitted = {k for k in LevelKind if k in (
        LevelKind.PREV_DAY_HIGH, LevelKind.PREV_DAY_LOW,
        LevelKind.PREV_WEEK_HIGH, LevelKind.PREV_WEEK_LOW,
        LevelKind.PREV_MONTH_HIGH, LevelKind.PREV_MONTH_LOW,
        LevelKind.ROUND_NUMBER, LevelKind.SWING_HIGH, LevelKind.SWING_LOW,
        LevelKind.SESSION_HIGH, LevelKind.SESSION_LOW, LevelKind.VWAP,
    )}
    seen_kinds = {lv.kind for lv in levels}
    assert seen_kinds.issubset(permitted)
