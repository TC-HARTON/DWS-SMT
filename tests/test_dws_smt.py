"""Verify the DWS-SMT port reproduces DWS_SMT.mq5 v2.00 behaviour.

The reference values are hand-computed from the .mq5 recursion directly, so
a mismatch here is a real port bug — not a floating-point quirk.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from analyzer import dws_smt
from analyzer.dws_smt import (
    COLOR_DOWN,
    COLOR_NEUTRAL,
    COLOR_UP,
    _bias_series,
    _colorize,
    _diff_series,
    _ema,
    _map_onto,
    _pair_trades,
    compute_symbol,
)

ALL_TFS = ("M15", "H1", "H4", "D1", "W1")


def _df(periods: int = 40, *, step: float = 1.0, start_price: float = 100.0,
        freq_min: int = 60, tz: str | None = "UTC") -> pd.DataFrame:
    """Build a synthetic OHLC frame with a linear close ramp."""
    idx = pd.date_range(
        "2024-01-01", periods=periods,
        freq=pd.Timedelta(minutes=freq_min), tz=tz,
    )
    close = start_price + step * np.arange(periods, dtype=float)
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close},
        index=idx,
    )


def _triggers(window) -> list[str | None]:
    return list(window.triggers)


# ----------------------------------------------------------------- _ema

def test_ema_first_value_seed():
    # seed = x[0] reproduces the .mq5 emaArr[0] = tfClose[0].
    out = _ema(np.array([10.0, 12.0, 14.0]), alpha=0.5, seed=10.0)
    # y0 = .5*10 + .5*10 = 10 ; y1 = .5*12 + .5*10 = 11 ; y2 = .5*14 + .5*11 = 12.5
    np.testing.assert_allclose(out, [10.0, 11.0, 12.5], rtol=1e-12)


def test_ema_zero_seed():
    # seed = 0 reproduces the .mq5 SmoothAndColor static sm = 0.
    out = _ema(np.array([10.0, 12.0, 14.0]), alpha=0.5, seed=0.0)
    # y0 = .5*10 ; y1 = .5*12 + .5*5 = 8.5 ; y2 = .5*14 + .5*8.5 = 11.25
    np.testing.assert_allclose(out, [5.0, 8.5, 11.25], rtol=1e-12)


# ----------------------------------------------------------------- _diff_series

def test_diff_series_matches_manual():
    df = _df(periods=4, step=1.0, start_price=100.0)   # closes 100,101,102,103
    _, diff = _diff_series(df, period=3)                # alpha 0.5, ema seed 100
    # ema = 100, 100.5, 101.25, 102.125  ->  diff = close - ema
    np.testing.assert_allclose(diff, [0.0, 0.5, 0.75, 0.875], rtol=1e-12)


# ----------------------------------------------------------------- _map_onto

def test_map_onto_step_function():
    base = np.array([1, 2, 3, 4, 5], dtype=np.int64)
    sub = np.array([2, 4], dtype=np.int64)
    sub_diff = np.array([100.0, 200.0])
    # base bar takes the most recent sub bar <= it; bars before the first -> 0.
    out = _map_onto(base, sub, sub_diff)
    np.testing.assert_array_equal(out, [0.0, 100.0, 100.0, 200.0, 200.0])


# ----------------------------------------------------------------- _colorize

def test_colorize_sign_mapping():
    out = _colorize(np.array([0.5, -0.5, 0.0]))
    np.testing.assert_array_equal(out, [COLOR_UP, COLOR_DOWN, COLOR_NEUTRAL])


# ----------------------------------------------------------------- end to end

def test_all_rising_yields_one_buy_and_green_rows():
    df = _df(periods=40, step=1.0)
    frames = {tf: df for tf in ALL_TFS}
    res = compute_symbol(frames, period=3, smooth=2, out_bars=40)
    assert res is not None
    win = res.by_base["M15"]
    trigs = _triggers(win)
    # A rising market aligns all three rows green -> exactly one BUY edge.
    assert trigs.count("BUY") == 1
    assert "SELL" not in trigs
    # The latest bar is fully green.
    assert win.colors[-1].tolist() == [COLOR_UP, COLOR_UP, COLOR_UP]


def test_all_falling_yields_sell_and_red_rows():
    df = _df(periods=40, step=-1.0, start_price=200.0)
    frames = {tf: df for tf in ALL_TFS}
    res = compute_symbol(frames, period=3, smooth=2, out_bars=40)
    win = res.by_base["M15"]
    trigs = _triggers(win)
    assert trigs.count("SELL") == 1
    assert "BUY" not in trigs
    assert win.colors[-1].tolist() == [COLOR_DOWN, COLOR_DOWN, COLOR_DOWN]


def _df_from_closes(closes, idx) -> pd.DataFrame:
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c}, index=idx)


def test_aligned_then_one_row_reverses_emits_buy_then_exit():
    # All three rows rise together (-> BUY), then H1 alone turns down. The
    # rows disagree -> state 0 -> EXIT. This is the multi-TF break the .mq5
    # EXIT arrow is built for.
    idx = pd.date_range("2024-01-01", periods=40,
                        freq=pd.Timedelta(hours=1), tz="UTC")
    rising = 100.0 + np.arange(40, dtype=float)
    h1_closes = np.concatenate([
        100.0 + np.arange(20, dtype=float),            # rise 100..119
        119.0 - np.arange(1, 21, dtype=float),         # fall 118..99
    ])
    frames = {
        "M15": _df_from_closes(rising, idx),           # base timeline only
        "H1":  _df_from_closes(h1_closes, idx),
        "H4":  _df_from_closes(rising, idx),
        "D1":  _df_from_closes(rising, idx),
    }
    res = compute_symbol(frames, period=3, smooth=2, out_bars=40)
    trigs = _triggers(res.by_base["M15"])
    assert "BUY" in trigs and "EXIT" in trigs
    # The BUY must precede the EXIT that ends the alignment.
    assert trigs.index("BUY") < trigs.index("EXIT")
    assert "SELL" not in trigs                         # D1/H4 never turn red


def test_no_trigger_on_oldest_or_inprogress_bar():
    df = _df(periods=40, step=1.0)
    frames = {tf: df for tf in ALL_TFS}
    res = compute_symbol(frames, period=3, smooth=2, out_bars=40)
    trigs = _triggers(res.by_base["M15"])
    # .mq5 guard: bar >= 1 (skip in-progress) and i > 0 (skip oldest).
    assert trigs[0] is None
    assert trigs[-1] is None


def test_missing_base_returns_none():
    # Only a row TF, no base TF -> nothing can be rendered.
    res = compute_symbol({"D1": _df(periods=30)}, period=3, smooth=2)
    assert res is None


def test_missing_row_is_treated_as_neutral():
    df = _df(periods=40, step=1.0)
    frames = {tf: df for tf in ("M15", "H1")}        # H4 row absent
    res = compute_symbol(frames, period=3, smooth=2, out_bars=40)
    assert res is not None
    win = res.by_base["M15"]
    # M15 stack rows = (H4, H1, M15); the absent H4 row (column 0) stays
    # neutral on every bar, so the three rows never align -> no BUY.
    assert win.rows == ("H4", "H1", "M15")
    assert (win.colors[:, 0] == COLOR_NEUTRAL).all()
    assert "BUY" not in _triggers(win)


def test_each_base_anchors_its_own_stack():
    df = _df(periods=40, step=1.0)
    frames = {tf: df for tf in ALL_TFS}
    res = compute_symbol(frames, period=3, smooth=2, out_bars=40)
    # The selected base TF is the bottom row; the two next-higher timeframes
    # stack above it. Switching the base slides the stack up the TF ladder.
    assert res.by_base["M15"].rows == ("H4", "H1", "M15")
    assert res.by_base["H1"].rows == ("D1", "H4", "H1")
    assert res.by_base["H4"].rows == ("W1", "D1", "H4")


def test_out_bars_limits_emitted_window():
    df = _df(periods=80, step=1.0)
    frames = {tf: df for tf in ALL_TFS}
    res = compute_symbol(frames, period=3, smooth=2, out_bars=30)
    win = res.by_base["M15"]
    assert win.colors.shape == (30, 3)
    assert len(win.triggers) == 30
    assert win.times_ms.shape == (30,)
    assert win.bias.shape == (30,)
    assert isinstance(win.trades, tuple)


# ----------------------------------------------------- _bias_series (no look-ahead)

def test_bias_series_empty_contrib_is_zero():
    base = np.array([10, 20, 30], dtype=np.int64)
    np.testing.assert_array_equal(_bias_series(base, None), [0.0, 0.0, 0.0])


def test_bias_series_weighted_composite():
    # One TF (H4, weight 2.0) contributing +1 on every base bar. Only H4 is
    # present → max |score| = 2 × 2 → composite = contrib / (2 × Σw) × 10.
    base = np.array([10, 20, 30], dtype=np.int64)
    contrib = {"H4": (np.array([5, 15, 25], dtype=np.int64),
                      np.array([1.0, 1.0, 1.0]))}
    out = _bias_series(base, contrib)
    np.testing.assert_allclose(out, [5.0, 5.0, 5.0])   # 1·2 / (2·2) · 10


# ----------------------------------------------------- _pair_trades (back-test)

def test_pair_trades_long_win_then_loss():
    # BUY@100 → EXIT@110 (long +10); BUY@110 → EXIT@104 (long -6).
    trigs = (None, "BUY", None, "EXIT", "BUY", None, "EXIT", None)
    closes = np.array([99, 100, 105, 110, 110, 108, 104, 103], dtype=float)
    trades = _pair_trades(trigs, closes, closes, closes)
    assert len(trades) == 2
    assert trades[0].direction == 1 and trades[0].entry_idx == 1
    assert trades[0].points == pytest.approx(10.0) and not trades[0].is_open
    assert trades[1].points == pytest.approx(-6.0)


def test_pair_trades_short_profits_as_price_falls():
    trigs = (None, "SELL", None, "EXIT")
    closes = np.array([101, 100, 96, 92], dtype=float)
    trades = _pair_trades(trigs, closes, closes, closes)
    assert len(trades) == 1
    assert trades[0].direction == -1
    assert trades[0].points == pytest.approx(8.0)        # 100 → 92, short = +8


def test_pair_trades_reversal_closes_and_opens():
    # BUY@100 → SELL@108: long closes (+8), short opens and stays open.
    trigs = (None, "BUY", None, "SELL", None)
    closes = np.array([99, 100, 104, 108, 106], dtype=float)
    trades = _pair_trades(trigs, closes, closes, closes)
    assert len(trades) == 2
    assert trades[0].direction == 1 and trades[0].points == pytest.approx(8.0)
    assert not trades[0].is_open
    assert trades[1].direction == -1 and trades[1].is_open
    assert trades[1].points == pytest.approx(2.0)         # floating: 108 → 106


def test_pair_trades_open_trade_floats_to_last_close():
    trigs = (None, "BUY", None, None)
    closes = np.array([99, 100, 103, 107], dtype=float)
    trades = _pair_trades(trigs, closes, closes, closes)
    assert len(trades) == 1
    assert trades[0].is_open and trades[0].points == pytest.approx(7.0)


def test_pair_trades_orphan_exit_ignored():
    # A leading EXIT closes a trade entered before the window — no open
    # position here, so it is ignored.
    trigs = (None, "EXIT", None, "BUY", "EXIT")
    closes = np.array([100, 101, 102, 103, 109], dtype=float)
    trades = _pair_trades(trigs, closes, closes, closes)
    assert len(trades) == 1
    assert trades[0].entry_idx == 3 and trades[0].points == pytest.approx(6.0)


def test_pair_trades_records_mae():
    # Long BUY@100 → EXIT@105; price dipped to a low of 94 mid-trade.
    trigs = (None, "BUY", None, "EXIT")
    closes = np.array([99, 100, 102, 105], dtype=float)
    highs = np.array([100, 101, 103, 106], dtype=float)
    lows = np.array([98, 100, 94, 103], dtype=float)
    trades = _pair_trades(trigs, closes, highs, lows)
    assert trades[0].points == pytest.approx(5.0)
    assert trades[0].mae == pytest.approx(6.0)        # 100 entry − 94 low


def test_pair_trades_mae_zero_when_never_adverse():
    # A long that only ever rose → MAE is 0 (never underwater).
    trigs = (None, "BUY", None, "EXIT")
    closes = np.array([99, 100, 110, 120], dtype=float)
    highs = np.array([100, 101, 111, 121], dtype=float)
    lows = np.array([98, 100, 109, 119], dtype=float)
    trades = _pair_trades(trigs, closes, highs, lows)
    assert trades[0].mae == pytest.approx(0.0)
