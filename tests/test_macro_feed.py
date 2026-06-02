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
    # Same-rate non-JPY pair: differential = 0 → macro_dir 0 (deadband).
    assert snap.by_pair["EURUSD"].macro_dir == 0          # 4.0 - 4.0 == 0
    assert "USD" in snap.rates
    # A partial failure yields a usable snapshot — it is NOT a failure cycle,
    # so the consecutive-failure counter stays 0; the error is still recorded.
    assert snap.consecutive_failures == 0
    assert snap.last_error is not None


def test_macro_engine_total_failure_increments(monkeypatch, tmp_path):
    def all_down(ccy):
        raise ValueError(f"{ccy} down")
    eng = mf.MacroEngine(cache_file=tmp_path / "c.json")
    monkeypatch.setattr(eng, "_fetch_rate", all_down)
    monkeypatch.setattr(eng, "_fetch_employment", lambda: None)
    snap1 = eng.compute()
    snap2 = eng.compute()
    # Every source down → each cycle is a real failure cycle.
    assert snap1.consecutive_failures == 1
    assert snap2.consecutive_failures == 2
    assert snap2.last_error is not None


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


# ------------------------------------------------ last-good cache / resilience
def _emp(nfp=115.0, unrate=4.3):
    return mf.MacroEmployment(nonfarm_change=nfp, unemployment_rate=unrate,
                              as_of="2026-05-01", prev_nonfarm_change=100.0,
                              source="fred")


def test_employment_failure_serves_stale_cache(monkeypatch, tmp_path):
    """A later employment-fetch failure re-uses the last-good reading (stale)
    instead of blanking the row."""
    eng = mf.MacroEngine(cache_file=tmp_path / "c.json")
    monkeypatch.setattr(eng, "_fetch_rate",
                        lambda ccy: mf.MacroRate(ccy, 4.0, "2026-05-01", None, "t", False))
    monkeypatch.setattr(eng, "_fetch_employment", lambda: _emp())
    snap1 = eng.compute()
    assert snap1.employment is not None and snap1.employment.stale is False

    def boom():
        raise ValueError("payems down")
    monkeypatch.setattr(eng, "_fetch_employment", boom)
    snap2 = eng.compute()
    assert snap2.employment is not None                 # not blanked
    assert snap2.employment.stale is True               # flagged stale
    assert snap2.employment.nonfarm_change == 115.0      # last-good value
    assert snap2.last_error is not None


def test_cache_survives_restart(monkeypatch, tmp_path):
    """Employment + real-yield persist to disk and are restored (stale) by a
    fresh engine — so a restart during a FRED outage still shows them."""
    cache = tmp_path / "macro_cache.json"
    eng = mf.MacroEngine(cache_file=cache)
    monkeypatch.setattr(eng, "_fetch_rate",
                        lambda ccy: mf.MacroRate(ccy, 4.0, "2026-05-01", None, "t", False))
    monkeypatch.setattr(eng, "_fetch_employment", lambda: _emp())
    eng.compute()
    obs = [{"date": f"2026-05-{d:02d}", "value": "2.10"} for d in range(1, 13)]
    monkeypatch.setattr(eng, "_fred_get", lambda sid, limit=6: _json.dumps({"observations": obs}))
    eng.fetch_real_yield()

    # Fresh engine on the SAME cache file = simulated restart (bootstrap in __init__).
    eng2 = mf.MacroEngine(cache_file=cache)
    assert eng2._cached_employment is not None
    assert eng2._cached_employment.stale is True
    assert eng2._cached_employment.nonfarm_change == 115.0
    assert eng2._cached_real_yield is not None
    assert eng2._cached_real_yield.stale is True
    assert eng2._cached_real_yield.value == pytest.approx(2.10)


def test_redact_strips_fred_api_key():
    """The FRED api_key must never survive into a log / error string."""
    msg = ("504 Server Error: Gateway Time-out for url: "
           "https://api.stlouisfed.org/fred/series/observations"
           "?series_id=DFII10&api_key=abc123SECRETkey&file_type=json")
    out = mf._redact(msg)
    assert "abc123SECRETkey" not in out
    assert "api_key=***" in out
    assert "series_id=DFII10" in out      # non-secret params preserved


def test_parse_fred_series_returns_chronological_levels():
    body = _json.dumps({"observations": [
        {"date": "2026-05-29", "value": "2.10"},
        {"date": "2026-05-28", "value": "."},      # missing → skipped
        {"date": "2026-05-27", "value": "2.00"},
    ]})
    as_of, levels = mf.parse_fred_series(body)
    # Sorted oldest→newest, missing dropped, newest date returned as as_of.
    assert as_of == "2026-05-29"
    assert levels == [2.00, 2.10]


def test_parse_fred_series_raises_on_empty():
    body = _json.dumps({"observations": [{"date": "2026-05-29", "value": "."}]})
    with pytest.raises(ValueError):
        mf.parse_fred_series(body)


def _fred_series_body(values: list[float], start="2025-01-01") -> str:
    import datetime
    d0 = datetime.date.fromisoformat(start)
    obs = [{"date": (d0 + datetime.timedelta(days=i)).isoformat(),
            "value": f"{v}"} for i, v in enumerate(values)]
    # FRED is fetched sort_order=desc; emulate newest-first.
    return _json.dumps({"observations": list(reversed(obs))})


def test_fetch_gold_drivers_returns_all_present(monkeypatch, tmp_path):
    eng = mf.MacroEngine(cache_file=tmp_path / "c.json")

    def fake_fred_get(series_id, limit=6):
        return _fred_series_body([1.0, 2.0, 3.0])
    monkeypatch.setattr(eng, "_fred_get", fake_fred_get)

    histories, as_of, n_fresh = eng.fetch_gold_drivers()
    from analyzer import gold_macro as gm
    assert set(histories) == {d.key for d in gm.GOLD_DRIVERS}
    assert histories["vix"] == [1.0, 2.0, 3.0]
    assert as_of == "2025-01-03"
    assert n_fresh == len(gm.GOLD_DRIVERS)        # all four fetched fresh


def test_fetch_gold_drivers_omits_failed_series_with_no_cache(monkeypatch, tmp_path):
    import requests
    from analyzer import gold_macro as gm
    eng = mf.MacroEngine(cache_file=tmp_path / "c.json")

    def fake_fred_get(series_id, limit=6):
        if series_id == cfg_dxy():
            raise requests.RequestException("boom")
        return _fred_series_body([1.0, 2.0, 3.0])
    monkeypatch.setattr(eng, "_fred_get", fake_fred_get)

    histories, _, n_fresh = eng.fetch_gold_drivers()
    # With NO prior cache, a failed driver cannot be filled → it is absent.
    assert "dxy" not in histories
    assert "vix" in histories
    assert n_fresh == len(gm.GOLD_DRIVERS) - 1     # three of four fresh


def test_fetch_gold_drivers_falls_back_to_cached_driver_on_partial_failure(monkeypatch, tmp_path):
    """A transient single-driver failure must NOT drop that driver if a prior
    good history is cached — it falls back to the cache so the composite stays
    on all four drivers (no score swing)."""
    import requests
    from analyzer import gold_macro as gm
    eng = mf.MacroEngine(cache_file=tmp_path / "c.json")

    # First call: every driver succeeds → cache now holds all four.
    monkeypatch.setattr(eng, "_fred_get",
                        lambda series_id, limit=6: _fred_series_body([1.0, 2.0, 3.0]))
    first, _, n_fresh_1 = eng.fetch_gold_drivers()
    assert n_fresh_1 == len(gm.GOLD_DRIVERS)
    assert first["dxy"] == [1.0, 2.0, 3.0]

    # Second call: DXY now fails. It must be served from cache, not dropped.
    def flaky(series_id, limit=6):
        if series_id == cfg_dxy():
            raise requests.RequestException("429")
        return _fred_series_body([4.0, 5.0, 6.0])
    monkeypatch.setattr(eng, "_fred_get", flaky)

    second, _, n_fresh_2 = eng.fetch_gold_drivers()
    assert "dxy" in second                          # NOT dropped
    assert second["dxy"] == [1.0, 2.0, 3.0]         # served from the first call's cache
    assert second["vix"] == [4.0, 5.0, 6.0]         # the fresh ones updated
    assert n_fresh_2 == len(gm.GOLD_DRIVERS) - 1    # only three fetched fresh


def test_fetch_gold_drivers_partial_success_does_not_clobber_cache(monkeypatch, tmp_path):
    """A partial-success fetch must MERGE into the cache, never wholesale
    replace it (which would discard a previously-good driver)."""
    import requests
    eng = mf.MacroEngine(cache_file=tmp_path / "c.json")

    monkeypatch.setattr(eng, "_fred_get",
                        lambda series_id, limit=6: _fred_series_body([1.0, 2.0, 3.0]))
    eng.fetch_gold_drivers()                         # seed cache with all four

    def only_vix(series_id, limit=6):
        import config
        if series_id == config.MACRO_FRED_VIX_SERIES:
            return _fred_series_body([9.0, 9.0, 9.0])
        raise requests.RequestException("down")
    monkeypatch.setattr(eng, "_fred_get", only_vix)
    eng.fetch_gold_drivers()                         # only VIX fresh; others must persist

    # The on-disk cache must still carry all four drivers (merge, not replace).
    cached = eng._cached_gold_drivers
    from analyzer import gold_macro as gm
    assert set(cached) == {d.key for d in gm.GOLD_DRIVERS}
    assert cached["vix"] == [9.0, 9.0, 9.0]          # refreshed
    assert cached["dxy"] == [1.0, 2.0, 3.0]          # preserved from the seed


def test_fetch_gold_drivers_total_failure_serves_cache(monkeypatch, tmp_path):
    """When every driver fails, the merged view falls back entirely to cache and
    n_fresh is 0 (caller flags stale + retries soon)."""
    import requests
    from analyzer import gold_macro as gm
    eng = mf.MacroEngine(cache_file=tmp_path / "c.json")

    monkeypatch.setattr(eng, "_fred_get",
                        lambda series_id, limit=6: _fred_series_body([1.0, 2.0, 3.0]))
    eng.fetch_gold_drivers()                         # seed cache

    def all_fail(series_id, limit=6):
        raise requests.RequestException("FRED down")
    monkeypatch.setattr(eng, "_fred_get", all_fail)
    histories, _, n_fresh = eng.fetch_gold_drivers()
    assert set(histories) == {d.key for d in gm.GOLD_DRIVERS}   # all served from cache
    assert n_fresh == 0


def cfg_dxy():
    import config
    return config.MACRO_FRED_DXY_SERIES
