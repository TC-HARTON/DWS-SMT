"""Tests for the signal-validation layer."""

from __future__ import annotations

import math

import numpy as np
import pytest

from analyzer import signal_validator as sv
from analyzer.dws_smt import DwsSmtTrade


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


# --------------------------------------------------------------- breakeven
def test_breakeven_win_rate_symmetric():
    # avg win == avg loss magnitude → breakeven at 50 %.
    assert sv.breakeven_win_rate([100.0, -100.0, 100.0]) == pytest.approx(0.5)


def test_breakeven_win_rate_no_losses():
    assert sv.breakeven_win_rate([10.0, 20.0]) == pytest.approx(0.0)


def test_breakeven_win_rate_no_wins():
    assert sv.breakeven_win_rate([-10.0, -20.0]) == pytest.approx(1.0)


# -------------------------------------------------------------------- tier
def test_classify_tier_insufficient():
    assert sv.classify_tier(n_trades=10, ci_low=0.9, breakeven=0.5,
                             thirds_expectancy=[1.0, 1.0, 1.0]) == "データ不足"


def test_classify_tier_trusted():
    # enough trades, CI lower bound clears breakeven, all thirds positive.
    assert sv.classify_tier(n_trades=50, ci_low=0.6, breakeven=0.5,
                            thirds_expectancy=[1.0, 2.0, 0.5]) == "信頼"


def test_classify_tier_caution_unstable():
    # enough trades but one third has negative expectancy.
    assert sv.classify_tier(n_trades=50, ci_low=0.6, breakeven=0.5,
                            thirds_expectancy=[1.0, -2.0, 0.5]) == "要注意"


def test_classify_tier_caution_ci_below_breakeven():
    assert sv.classify_tier(n_trades=50, ci_low=0.45, breakeven=0.5,
                            thirds_expectancy=[1.0, 1.0, 1.0]) == "要注意"


# --------------------------------------------------------------- evaluate
def _trade(entry_idx, direction, points, mae=0.0, is_open=False):
    return DwsSmtTrade(entry_idx=entry_idx, direction=direction,
                       points=points, mae=mae, is_open=is_open)


def test_evaluate_trades_skips_open_trade():
    # 1 closed winner + 1 open trade → only the closed one counts.
    trades = (_trade(0, 1, 1.0), _trade(2, 1, 5.0, is_open=True))
    spread = np.zeros(4)
    adx = np.full(4, 30.0)
    core = sv.evaluate_trades(trades, spread_pts=spread, adx=adx, point=1.0)
    assert core.n_trades == 1


def test_evaluate_trades_cost_is_deducted():
    # raw +10 price points, point=1.0, spread 3 pts at entry → net 7.
    trades = (_trade(0, 1, 10.0),)
    spread = np.array([3.0, 0.0])
    adx = np.array([30.0, 30.0])
    core = sv.evaluate_trades(trades, spread_pts=spread, adx=adx, point=1.0)
    assert core.expectancy == pytest.approx(7.0)


def test_evaluate_trades_regime_split():
    # entry 0 in a trend bar (ADX 30), entry 1 in a range bar (ADX 10).
    trades = (_trade(0, 1, 10.0), _trade(1, 1, -4.0))
    spread = np.zeros(2)
    adx = np.array([30.0, 10.0])
    core = sv.evaluate_trades(trades, spread_pts=spread, adx=adx, point=1.0)
    assert core.regime_trend.n_trades == 1
    assert core.regime_range.n_trades == 1
    assert core.regime_trend.expectancy == pytest.approx(10.0)
    assert core.regime_range.expectancy == pytest.approx(-4.0)


def test_evaluate_trades_empty_is_insufficient():
    core = sv.evaluate_trades((), spread_pts=np.zeros(1),
                              adx=np.zeros(1), point=1.0)
    assert core.n_trades == 0
    assert core.tier == "データ不足"


def test_evaluate_trades_thirds_split():
    # 30 identical winners → all three thirds have 10 trades, all positive.
    trades = tuple(_trade(i, 1, 2.0) for i in range(30))
    spread = np.zeros(31)
    adx = np.full(31, 30.0)
    core = sv.evaluate_trades(trades, spread_pts=spread, adx=adx, point=1.0)
    assert [t.n_trades for t in core.thirds] == [10, 10, 10]
    assert core.tier == "信頼"
