"""Unit tests for analyzer.currency_strength (SPEC §12)."""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

import config
from analyzer.currency_strength import (
    CurrencyStrengthEngine,
    PairBias,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _make_rate_df(close_prev: float, close_cur: float) -> pd.DataFrame:
    # 6 bars. The engine measures the cumulative % change over the last
    # 3 closed bars: reference = close[-5], endpoint = close[-2], with the
    # in-progress bar[-1] ignored. Lay the closes out so close[-5]=prev
    # and close[-2]=cur ⇒ the test's intended (cur-prev)/prev change holds.
    closes = [close_prev, close_prev, close_prev, close_prev, close_cur, close_cur]
    idx = pd.date_range("2026-01-01", periods=6, freq="1h", tz="UTC")
    return pd.DataFrame({
        "open":  closes,
        "high":  closes,
        "low":   closes,
        "close": closes,
        "tick_volume": [1] * 6, "spread": [0] * 6, "real_volume": [0] * 6,
    }, index=idx)


def _stub_connector(prices: dict[str, tuple[float, float]]) -> MagicMock:
    """Connector double whose fetch_rates_parallel returns the given prices
    for every configured window."""
    conn = MagicMock()
    conn.resolve_optional.return_value = {p: p for p in prices.keys()}

    def fake_fetch(symbols, windows):
        out: dict[tuple[str, str], pd.DataFrame] = {}
        for s in symbols:
            if s not in prices:
                continue
            for w in windows:
                out[(s, w.label)] = _make_rate_df(*prices[s])
        return out
    conn.fetch_rates_parallel.side_effect = fake_fetch
    return conn


# --------------------------------------------------------------------------- #
# Pair split
# --------------------------------------------------------------------------- #

def test_split_pair_six_char():
    assert CurrencyStrengthEngine._split_pair("EURUSD") == ("EUR", "USD")
    assert CurrencyStrengthEngine._split_pair("USDJPY") == ("USD", "JPY")


def test_split_pair_xau():
    assert CurrencyStrengthEngine._split_pair("XAUUSD") == ("XAU", "USD")


def test_split_pair_invalid_raises():
    with pytest.raises(ValueError):
        CurrencyStrengthEngine._split_pair("WEIRD")


# --------------------------------------------------------------------------- #
# Pair-bias classifier
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("delta,expected", [
    (40, "STRONG BUY"),
    (15, "BUY"),
    (5, "NEUTRAL"),
    (-5, "NEUTRAL"),
    (-15, "SELL"),
    (-40, "STRONG SELL"),
])
def test_classify_bias_thresholds(delta, expected):
    assert CurrencyStrengthEngine._classify_bias(delta) == expected


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #

def test_normalise_zscore_centres_on_mean():
    # Z-score normalisation: the cross-sectional mean maps to 50, and the
    # distribution is symmetric around it. avgs symmetric about 0 ⇒ EUR=50,
    # USD/JPY mirror each other.
    avgs = {"USD": -1.0, "EUR": 0.0, "JPY": +1.0}
    cnt = {"USD": 5, "EUR": 4, "JPY": 6}
    scores = CurrencyStrengthEngine._normalise(avgs, cnt)
    assert scores["EUR"].score == pytest.approx(50.0)
    assert scores["USD"].score < 50.0 < scores["JPY"].score
    # Symmetric inputs ⇒ scores mirror around 50.
    assert scores["USD"].score == pytest.approx(100.0 - scores["JPY"].score)
    # All scores stay within the 0..100 band.
    for s in scores.values():
        assert 0.0 <= s.score <= 100.0


def test_normalise_all_equal_returns_midline():
    avgs = {"USD": 0.5, "EUR": 0.5, "JPY": 0.5}
    cnt = {"USD": 1, "EUR": 1, "JPY": 1}
    scores = CurrencyStrengthEngine._normalise(avgs, cnt)
    for s in scores.values():
        assert s.score == pytest.approx(50.0)


# --------------------------------------------------------------------------- #
# End-to-end compute
# --------------------------------------------------------------------------- #

def test_compute_emits_one_window_block_per_configured_window():
    # Build a minimal price map covering at least 1 pair per currency we expect.
    prices = {
        "EURUSD": (1.10, 1.11),     # EUR ↑
        "USDJPY": (150.0, 149.0),   # USD ↓
        "GBPUSD": (1.30, 1.29),     # GBP ↓
        "AUDUSD": (0.65, 0.66),     # AUD ↑
        "USDCHF": (0.90, 0.91),     # USD ↑ vs CHF
        "USDCAD": (1.35, 1.36),     # USD ↑ vs CAD
        "NZDUSD": (0.60, 0.61),     # NZD ↑
        "XAUUSD": (3000.0, 3010.0), # XAU ↑
    }
    conn = _stub_connector(prices)
    eng = CurrencyStrengthEngine(connector=conn, pairs=tuple(prices.keys()))
    eng.resolve_pairs()
    snap = eng.compute()
    assert set(snap.by_window) == {w.label for w in config.STRENGTH_WINDOWS}
    for window_result in snap.by_window.values():
        # All listed display currencies (or those that received contributions)
        # should land on the 0..100 scale.
        for sc in window_result.scores.values():
            assert 0.0 <= sc.score <= 100.0


def test_pair_bias_uses_correct_split_for_display_pairs():
    # Construct prices so USD is uniformly weakest, EUR strongest.
    prices = {
        "EURUSD": (1.0, 1.1),       # EUR ↑↑
        "USDJPY": (150, 140),       # USD ↓↓
        "GBPUSD": (1.3, 1.31),      # tiny tick
        "AUDUSD": (0.65, 0.651),
        "USDCHF": (0.9, 0.901),
        "USDCAD": (1.35, 1.351),
        "NZDUSD": (0.6, 0.601),
    }
    conn = _stub_connector(prices)
    eng = CurrencyStrengthEngine(
        connector=conn, pairs=tuple(prices.keys()),
        display_pairs=("EURUSD", "USDJPY"),
    )
    eng.resolve_pairs()
    snap = eng.compute()
    for w in snap.by_window.values():
        eu = w.pair_biases.get("EURUSD")
        uj = w.pair_biases.get("USDJPY")
        assert isinstance(eu, PairBias)
        assert isinstance(uj, PairBias)
        # EUR strong, USD weak ⇒ EURUSD biased BUY.
        assert eu.delta > 0
        # USD weak, JPY strong (USDJPY fell hard) ⇒ USDJPY biased SELL.
        assert uj.delta < 0


def test_compute_handles_no_available_pairs_gracefully():
    conn = MagicMock()
    conn.resolve_optional.return_value = {}
    eng = CurrencyStrengthEngine(connector=conn)
    snap = eng.compute()
    assert snap.by_window == {}


def test_compute_skips_pair_with_insufficient_bars():
    # Only one bar — pct change cannot be computed.
    conn = MagicMock()
    conn.resolve_optional.return_value = {"EURUSD": "EURUSD"}
    one_bar = pd.DataFrame({
        "open": [1.1], "high": [1.1], "low": [1.1], "close": [1.1],
        "tick_volume": [1], "spread": [0], "real_volume": [0],
    }, index=pd.date_range("2026-01-01", periods=1, freq="1h", tz="UTC"))
    conn.fetch_rates_parallel.return_value = {("EURUSD", w.label): one_bar
                                              for w in config.STRENGTH_WINDOWS}
    eng = CurrencyStrengthEngine(connector=conn, pairs=("EURUSD",))
    eng.resolve_pairs()
    snap = eng.compute()
    # No contributions ⇒ no scores.
    for w in snap.by_window.values():
        assert w.scores == {}
