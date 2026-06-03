"""CFTC COT feed: parse_cot_rows derivation + engine cache fallback + serialize.

The pure parser (:func:`analyzer.cot_feed.parse_cot_rows`) is exercised with
synthetic Socrata rows so the net / week-change / percentile / extreme logic is
verified in isolation, with no network. The engine's stale-from-cache fallback
is checked by pointing it at a cache file and forcing a fetch failure.
"""

from __future__ import annotations

import json

import pytest

import config
from analyzer.cot_feed import (
    CotEngine,
    CotSnapshot,
    _pct_rank,
    parse_cot_rows,
)


def _row(date: str, nc_long: int, nc_short: int,
         c_long: int = 0, c_short: int = 0, oi: int = 0) -> dict:
    """Build one raw Socrata row (values are strings, as the API returns)."""
    return {
        "report_date_as_yyyy_mm_dd": f"{date}T00:00:00.000",
        "noncomm_positions_long_all": str(nc_long),
        "noncomm_positions_short_all": str(nc_short),
        "comm_positions_long_all": str(c_long),
        "comm_positions_short_all": str(c_short),
        "open_interest_all": str(oi),
    }


# ------------------------------------------------------------ _pct_rank

def test_pct_rank_extremes_and_empty():
    assert _pct_rank([], 5) is None
    # Max value → 100th percentile (inclusive <=).
    assert _pct_rank([1, 2, 3], 3) == pytest.approx(100.0)
    # Min value → 1/3 of the window is <= it.
    assert _pct_rank([1, 2, 3], 1) == pytest.approx(100.0 / 3)


# -------------------------------------------------------- parse_cot_rows

def test_parse_cot_rows_derivations():
    # Rows out of order on purpose — the parser must sort ascending by date.
    rows = [
        _row("2026-05-26", 200, 50, c_long=10, c_short=300, oi=1000),   # latest
        _row("2026-05-19", 180, 60, oi=900),                            # prior
        _row("2026-05-12", 150, 70, oi=800),
    ]
    snap = parse_cot_rows(rows)

    assert snap.report_date == "2026-05-26"
    assert snap.noncomm_long == 200 and snap.noncomm_short == 50
    assert snap.net == 150                       # 200 - 50
    assert snap.net_prev == 120                  # 180 - 60
    assert snap.net_change == 30                 # 150 - 120
    assert snap.comm_net == -290                 # 10 - 300
    assert snap.open_interest == 1000
    assert snap.net_pct_oi == pytest.approx(15.0)        # 150 / 1000 * 100
    assert snap.long_share == pytest.approx(80.0)        # 200 / 250 * 100
    assert snap.direction == 1                           # net long
    # Net history chronological: [80, 120, 150]; latest 150 is the max → 100th pct.
    assert snap.net_history == (80, 120, 150)
    assert snap.history_dates == ("2026-05-12", "2026-05-19", "2026-05-26")
    assert snap.pctile_1y == pytest.approx(100.0)
    assert snap.extreme == 1                              # >= COT_EXTREME_HIGH_PCT
    assert snap.stale is False and snap.last_error is None


def test_parse_cot_rows_low_extreme():
    # Latest net is the strict MINIMUM of a 20-week window → bottom-decile
    # (pctile = 1/20 = 5% <= COT_EXTREME_LOW_PCT) → low-end extreme (-1), even
    # though the absolute net is still positive (specs net long but de-risked).
    rows = [_row(f"2026-01-{d:02d}", 200 + d, 40) for d in range(1, 20)]  # nets 161..179
    rows.append(_row("2026-05-26", 110, 100))                            # latest net 10 (min)
    snap = parse_cot_rows(rows)
    assert snap.net == 10
    assert snap.direction == 1                  # still net long
    assert snap.pctile_1y == pytest.approx(5.0)         # 1 of 20 is <= the min
    assert snap.extreme == -1                   # <= COT_EXTREME_LOW_PCT


def test_parse_cot_rows_skips_unusable_and_raises_when_all_bad():
    # A row missing the non-commercial fields is skipped, not fatal.
    rows = [
        {"report_date_as_yyyy_mm_dd": "2026-05-26", "open_interest_all": "5"},
        _row("2026-05-19", 100, 40),
    ]
    snap = parse_cot_rows(rows)
    assert snap.report_date == "2026-05-19"
    assert snap.net == 60

    with pytest.raises(ValueError):
        parse_cot_rows([{"open_interest_all": "5"}])


# ----------------------------------------------------------- CotEngine

def test_engine_stale_from_cache_on_fetch_failure(tmp_path, monkeypatch):
    cache = tmp_path / "cot_cache.json"
    eng = CotEngine(cache_file=cache)

    # First compute: stub a successful fetch → snapshot persisted to cache.
    good_rows = [_row("2026-05-26", 200, 50, oi=1000),
                 _row("2026-05-19", 180, 60, oi=900)]
    monkeypatch.setattr(eng, "_fetch_rows", lambda: good_rows)
    fresh = eng.compute()
    assert fresh.stale is False and fresh.net == 150
    assert cache.exists()

    # Second compute: force a fetch failure → last-good reused, flagged stale.
    import requests as _rq

    def _boom():
        raise _rq.RequestException("network down")

    monkeypatch.setattr(eng, "_fetch_rows", _boom)
    stale = eng.compute()
    assert stale.stale is True
    assert stale.net == 150                      # value preserved from cache
    assert stale.last_error and "network down" in stale.last_error


def test_engine_bootstrap_from_disk_cache(tmp_path):
    cache = tmp_path / "cot_cache.json"
    cache.write_text(json.dumps({
        "market": config.COT_GOLD_MARKET, "report_date": "2026-05-26",
        "noncomm_long": 200, "noncomm_short": 50, "net": 150,
        "net_prev": 120, "net_change": 30, "comm_long": 10, "comm_short": 300,
        "comm_net": -290, "open_interest": 1000, "net_pct_oi": 15.0,
        "long_share": 80.0, "pctile_1y": 100.0, "direction": 1, "extreme": 1,
        "net_history": [80, 120, 150], "history_dates": ["a", "b", "c"],
        "fetched_at": 123.0,
    }), encoding="utf-8")

    eng = CotEngine(cache_file=cache)
    # Bootstrapped snapshot is exposed via the stale-fallback path.
    snap = eng._stale_from_cache("boot")
    assert snap.net == 150 and snap.stale is True
    assert snap.fetched_at == pytest.approx(123.0)


# ----------------------------------------------------------- serialize_cot

def test_serialize_cot_none():
    from dashboard.serialize import serialize_cot
    assert serialize_cot(None) is None


def test_serialize_cot_shape_is_valid_json():
    from dashboard.serialize import serialize_cot

    snap = parse_cot_rows([_row("2026-05-26", 200, 50, c_long=10, c_short=300, oi=1000),
                           _row("2026-05-19", 180, 60, oi=900)])
    out = serialize_cot(snap)
    for key in ("market", "report_date", "net", "net_change", "comm_net",
                "open_interest", "net_pct_oi", "long_share", "pctile_1y",
                "direction", "extreme", "net_history", "history_dates", "stale"):
        assert key in out
    assert out["net"] == 150
    assert out["net_history"] == [120, 150]      # chronological: 05-19 then 05-26
    # Must round-trip to valid JSON (no NaN/Inf leaking through).
    json.dumps(out)
