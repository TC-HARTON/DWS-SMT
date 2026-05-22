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


# ----------------------------------------------------------------- parsers
import pathlib

_FIX = pathlib.Path(__file__).parent / "fixtures" / "macro"


def test_parse_fred_json():
    body = ('{"observations":[{"date":"2026-03-01","value":"4.25"},'
            '{"date":"2026-04-01","value":"4.50"}]}')
    as_of, rate = mf.parse_fred_json(body)
    assert as_of == "2026-04-01"
    assert rate == pytest.approx(4.50)


def test_parse_fred_json_skips_missing():
    # FRED uses "." for a missing value — the parser must skip it.
    body = ('{"observations":[{"date":"2026-03-01","value":"4.25"},'
            '{"date":"2026-04-01","value":"."}]}')
    as_of, rate = mf.parse_fred_json(body)
    assert as_of == "2026-03-01"
    assert rate == pytest.approx(4.25)


def test_parse_ecb_csv():
    body = (_FIX / "ecb_sample.csv").read_text(encoding="utf-8")
    as_of, rate = mf.parse_ecb_csv(body)
    assert len(as_of) == 10 and as_of[4] == "-"      # ISO date
    assert isinstance(rate, float)


def test_parse_boe_csv():
    body = (_FIX / "boe_sample.csv").read_text(encoding="utf-8")
    as_of, rate = mf.parse_boe_csv(body)
    assert len(as_of) == 10 and as_of[4] == "-"
    assert isinstance(rate, float)


def test_parse_boj_html():
    body = (_FIX / "boj_sample.html").read_text(encoding="utf-8")
    as_of, rate = mf.parse_boj_html(body)
    assert len(as_of) == 10 and as_of[4] == "-"
    assert isinstance(rate, float)


# ------------------------------------------------------------------ engine
def test_macro_engine_compute_with_stub(monkeypatch, tmp_path):
    # Stub every HTTP fetch so the test is offline + deterministic.
    fake = {
        "USD": ("2026-05-01", 4.50), "EUR": ("2026-05-01", 2.00),
        "GBP": ("2026-05-01", 4.25), "JPY": ("2026-05-01", 0.50),
        "AUD": ("2026-05-01", 4.10),
    }
    eng = mf.MacroEngine(cache_file=tmp_path / "macro_cache.json")
    monkeypatch.setattr(eng, "_fetch_rate",
                        lambda ccy: mf.MacroRate(ccy, fake[ccy][1], fake[ccy][0],
                                                 None, "test", False))
    monkeypatch.setattr(eng, "_fetch_employment", lambda: None)
    snap = eng.compute()

    assert isinstance(snap, mf.MacroSnapshot)
    assert set(snap.rates) == {"USD", "EUR", "GBP", "JPY", "AUD"}
    assert "USDJPY" in snap.by_pair
    assert snap.by_pair["USDJPY"].macro_dir == 1          # 4.50 > 0.50
    assert snap.consecutive_failures == 0


def test_macro_engine_one_source_failure_is_isolated(monkeypatch, tmp_path):
    def flaky(ccy):
        if ccy == "JPY":
            raise ValueError("boj down")
        return mf.MacroRate(ccy, 4.0, "2026-05-01", None, "test", False)
    eng = mf.MacroEngine(cache_file=tmp_path / "macro_cache.json")
    monkeypatch.setattr(eng, "_fetch_rate", flaky)
    monkeypatch.setattr(eng, "_fetch_employment", lambda: None)
    snap = eng.compute()
    # JPY missing → pairs with JPY neutral; the other currencies unaffected.
    assert snap.by_pair["USDJPY"].macro_dir == 0
    assert snap.by_pair["EURGBP"].macro_dir == 0          # 4.0 - 4.0 == 0
    assert "USD" in snap.rates
    assert snap.consecutive_failures == 1


# --------------------------------------------------------------- real yield
import json as _json


def _ry_engine(monkeypatch, tmp_path, obs):
    eng = mf.MacroEngine(cache_file=tmp_path / "c.json")
    monkeypatch.setattr(eng, "_fred_get",
                        lambda sid, limit=6: _json.dumps({"observations": obs}))
    return eng


def test_fetch_real_yield_rising(monkeypatch, tmp_path):
    obs = [{"date": f"2026-05-{d:02d}", "value": f"{1.0 + d * 0.05:.4f}"}
           for d in range(1, 13)]
    ry = _ry_engine(monkeypatch, tmp_path, obs).fetch_real_yield()
    assert ry.value == pytest.approx(1.0 + 12 * 0.05)
    assert ry.change_1d == pytest.approx(0.05)
    assert ry.gold_dir == -1            # rising real yield → headwind for gold
    assert ry.stale is False


def test_fetch_real_yield_falling(monkeypatch, tmp_path):
    obs = [{"date": f"2026-05-{d:02d}", "value": f"{3.0 - d * 0.05:.4f}"}
           for d in range(1, 13)]
    ry = _ry_engine(monkeypatch, tmp_path, obs).fetch_real_yield()
    assert ry.gold_dir == 1             # falling real yield → tailwind for gold


def test_fetch_real_yield_flat(monkeypatch, tmp_path):
    obs = [{"date": f"2026-05-{d:02d}", "value": "2.00"} for d in range(1, 13)]
    ry = _ry_engine(monkeypatch, tmp_path, obs).fetch_real_yield()
    assert ry.gold_dir == 0


def test_fetch_real_yield_failure_is_stale(monkeypatch, tmp_path):
    eng = mf.MacroEngine(cache_file=tmp_path / "c.json")
    def boom(sid, limit=6):
        raise ValueError("fred down")
    monkeypatch.setattr(eng, "_fred_get", boom)
    ry = eng.fetch_real_yield()
    assert ry.stale is True
    assert ry.gold_dir == 0
