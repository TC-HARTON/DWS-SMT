"""IndicatorEngine: end-to-end indicator pass from synthetic rate frames."""

from __future__ import annotations

import numpy as np
import pandas as pd

import config
from analyzer.indicator_engine import IndicatorEngine, _bias_contribution_series


def _make_rate_df(n: int, base_price: float = 100.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed=42)
    drift = np.cumsum(rng.normal(0.0, 0.05, size=n))
    close = base_price + drift
    high = close + 0.20
    low = close - 0.20
    idx = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "tick_volume": np.full(n, 10),
            "spread": np.zeros(n, dtype=int),
            "real_volume": np.zeros(n, dtype=int),
        },
        index=idx,
    )


def test_engine_produces_one_snapshot_per_symbol():
    engine = IndicatorEngine()
    rates = {
        (s.base, tf.label): _make_rate_df(400)
        for s in config.SYMBOLS for tf in config.TIMEFRAMES
    }
    snap = engine.compute(rates)
    assert set(snap.by_symbol.keys()) == {s.base for s in config.SYMBOLS}
    for sym in snap.by_symbol.values():
        for tf in config.TIMEFRAMES:
            assert tf.label in sym.by_tf
            ind = sym.by_tf[tf.label]
            assert ind.ema is not None
            assert ind.rsi is not None
            assert ind.atr is not None
            assert ind.adx is not None


def test_engine_skips_missing_pairs():
    engine = IndicatorEngine()
    rates = {("XAUUSD", "H1"): _make_rate_df(200)}
    snap = engine.compute(rates)
    # Only XAUUSD has any data — but it must still have only the one TF.
    assert "XAUUSD" in snap.by_symbol
    assert set(snap.by_symbol["XAUUSD"].by_tf.keys()) == {"H1"}


def test_engine_compute_under_budget_for_full_load():
    """SPEC §19 mandates ≤ 50 ms; we allow 2× to absorb slower CI nodes."""
    engine = IndicatorEngine()
    rates = {
        (s.base, tf.label): _make_rate_df(tf.bars_to_fetch)
        for s in config.SYMBOLS for tf in config.TIMEFRAMES
    }
    snap = engine.compute(rates)
    budget = config.TARGET_ANALYSIS_BUDGET_MS * 2
    assert snap.compute_ms < budget, (
        f"compute took {snap.compute_ms:.1f} ms (>2x SPEC budget of "
        f"{config.TARGET_ANALYSIS_BUDGET_MS} ms)"
    )


def test_bias_contribution_series_ports_tfsignal_and_regime_gate():
    # All three bars above EMA. bar0 = STRONG BUY in a trend, bar1 = plain BUY
    # in a range, bar2 = NaN warmup.
    close = np.array([110.0, 110.0, 110.0])
    ema = np.array([100.0, 100.0, 100.0])
    rsi = np.array([60.0, 52.0, np.nan])
    adx = np.array([30.0, 20.0, 30.0])
    dip = np.array([25.0, 10.0, 25.0])
    dim = np.array([10.0, 12.0, 10.0])
    out = _bias_contribution_series(close, ema, rsi, adx, dip, dim)
    # bar0: code +2, ADX 30 → trend 1.0 → 2.0
    # bar1: code +1 (BUY), ADX 20 → trend 0.5 → 0.5
    # bar2: RSI NaN → 0.0
    np.testing.assert_allclose(out, [2.0, 0.5, 0.0])


def test_with_broker_names_patches_in_resolved_names():
    engine = IndicatorEngine()
    rates = {("XAUUSD", "H1"): _make_rate_df(200)}
    snap = engine.compute(rates)
    patched = IndicatorEngine.with_broker_names(snap, {"XAUUSD": "XAUUSDm"})
    assert patched.by_symbol["XAUUSD"].broker_name == "XAUUSDm"
    # Original untouched.
    assert snap.by_symbol["XAUUSD"].broker_name == "XAUUSD"
