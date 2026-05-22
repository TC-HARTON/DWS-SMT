"""Tests for the macro / rate-differential layer."""

from __future__ import annotations

import pytest

from analyzer import macro_feed as mf


def _rate(ccy, rate, prev=None, stale=False):
    return mf.MacroRate(currency=ccy, rate=rate, as_of="2026-05-22",
                        prev_rate=prev, source="test", stale=stale)


def test_pair_macro_bias_carry_direction():
    rates = {"USD": _rate("USD", 4.5), "JPY": _rate("JPY", 0.5)}
    b = mf.pair_macro_bias("USDJPY", rates)
    assert b.base_ccy == "USD" and b.quote_ccy == "JPY"
    assert b.differential == pytest.approx(4.0)
    assert b.macro_dir == 1            # USD out-yields JPY → carry favours USDJPY up


def test_pair_macro_bias_negative_carry():
    rates = {"EUR": _rate("EUR", 2.0), "GBP": _rate("GBP", 4.0)}
    b = mf.pair_macro_bias("EURGBP", rates)
    assert b.differential == pytest.approx(-2.0)
    assert b.macro_dir == -1


def test_pair_macro_bias_stale_currency_is_neutral():
    rates = {"USD": _rate("USD", 4.5, stale=True), "JPY": _rate("JPY", 0.5)}
    b = mf.pair_macro_bias("USDJPY", rates)
    assert b.macro_dir == 0            # never penalise on stale data


def test_pair_macro_bias_xauusd_uses_us_rate_trend():
    # Gold has no rate. Rising US rate → bearish gold → macro_dir -1.
    rates = {"USD": _rate("USD", 4.5, prev=4.25)}
    b = mf.pair_macro_bias("XAUUSD", rates)
    assert b.macro_dir == -1
    # Falling US rate → bullish gold.
    rates2 = {"USD": _rate("USD", 4.0, prev=4.25)}
    assert mf.pair_macro_bias("XAUUSD", rates2).macro_dir == 1
    # Flat → neutral.
    rates3 = {"USD": _rate("USD", 4.25, prev=4.25)}
    assert mf.pair_macro_bias("XAUUSD", rates3).macro_dir == 0


def test_pair_macro_bias_missing_currency_is_neutral():
    b = mf.pair_macro_bias("USDJPY", {"USD": _rate("USD", 4.5)})  # no JPY
    assert b.macro_dir == 0
