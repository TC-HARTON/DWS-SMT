"""OANDA sentiment layer — parse + engine + token gating tests."""

from __future__ import annotations

import json

import pytest

from analyzer import oanda_sentiment as os_mod
from analyzer.oanda_sentiment import (
    PairSentiment,
    SentimentEngine,
    SentimentSnapshot,
    parse_position_book,
)


# --------------------------------------------------------------- parse_position_book

def _make_book(market: float, buckets: list[dict]) -> str:
    return json.dumps({"positionBook": {"price": str(market), "buckets": buckets}})


def test_parse_short_squeeze_when_longs_winning_shorts_losing():
    # Both sides entered at 95; market climbed to 100.
    # Longs +5 (winning), shorts -5 (in loss) → short squeeze.
    body = _make_book(100.0, [
        {"price": "95.0", "longCountPercent": "100", "shortCountPercent": "100"},
    ])
    p = parse_position_book(body, "USDJPY")
    assert p is not None
    assert p.long_avg == pytest.approx(95.0)
    assert p.short_avg == pytest.approx(95.0)
    assert p.long_pnl == pytest.approx(5.0)
    assert p.short_pnl == pytest.approx(-5.0)
    assert p.bias == "short_squeeze"


def test_parse_long_squeeze_when_shorts_winning():
    # Both sides entered at 105; market dropped to 100.
    # Longs -5 (in loss), shorts +5 (winning) → long squeeze.
    body = _make_book(100.0, [
        {"price": "105.0", "longCountPercent": "100", "shortCountPercent": "100"},
    ])
    p = parse_position_book(body, "USDJPY")
    assert p is not None
    assert p.bias == "long_squeeze"
    assert p.long_pnl == pytest.approx(-5.0)
    assert p.short_pnl == pytest.approx(5.0)


def test_parse_neutral_when_both_sides_on_profitable_side():
    # Longs avg 99 (mkt 100, +1 winning); shorts avg 101 (mkt 100, +1 winning).
    # Both winning → no squeeze pressure → neutral.
    body = _make_book(100.0, [
        {"price": "99.0",  "longCountPercent": "100", "shortCountPercent": "0"},
        {"price": "101.0", "longCountPercent": "0",   "shortCountPercent": "100"},
    ])
    p = parse_position_book(body, "EURUSD")
    assert p is not None
    assert p.long_pnl == pytest.approx(1.0)
    assert p.short_pnl == pytest.approx(1.0)
    assert p.bias == "neutral"


def test_parse_returns_none_on_empty_buckets():
    body = json.dumps({"positionBook": {"price": "1.0", "buckets": []}})
    assert parse_position_book(body, "EURUSD") is None


def test_parse_returns_none_on_malformed_json():
    assert parse_position_book("not json", "EURUSD") is None


def test_parse_returns_none_when_market_price_missing():
    body = json.dumps({"positionBook": {"buckets": [
        {"price": "1.0", "longCountPercent": "100", "shortCountPercent": "100"}]}})
    assert parse_position_book(body, "EURUSD") is None


def test_parse_skips_buckets_with_bad_numeric_strings():
    body = _make_book(100.0, [
        {"price": "nope",  "longCountPercent": "50",  "shortCountPercent": "50"},
        {"price": "99.0",  "longCountPercent": "50",  "shortCountPercent": "50"},
        {"price": "101.0", "longCountPercent": "50",  "shortCountPercent": "50"},
    ])
    p = parse_position_book(body, "USDJPY")
    assert p is not None
    # Only the two well-formed buckets survive; weighted mean = (99*50 + 101*50)/100 = 100.
    assert p.long_avg == pytest.approx(100.0)


# --------------------------------------------------------------- engine

def test_engine_short_circuits_without_token():
    eng = SentimentEngine(api_token="", symbol_map={"USDJPY": "USD_JPY"})
    snap = eng.compute()
    assert isinstance(snap, SentimentSnapshot)
    assert snap.by_symbol == {}
    assert snap.last_error == "OANDA_API_TOKEN is not set"
    assert snap.consecutive_failures == 0    # no token ≠ failure cycle


def test_engine_fetches_with_stubbed_http(monkeypatch):
    eng = SentimentEngine(api_token="dummy",
                          symbol_map={"USDJPY": "USD_JPY", "EURUSD": "EUR_USD"})
    fake_bodies = {
        # USD/JPY: both sides at 148, market 150 → longs winning, shorts losing.
        "USD_JPY": _make_book(150.0, [
            {"price": "148.0", "longCountPercent": "100", "shortCountPercent": "100"},
        ]),
        # EUR/USD: both sides at 1.11, market 1.10 → longs losing, shorts winning.
        "EUR_USD": _make_book(1.10, [
            {"price": "1.11", "longCountPercent": "100", "shortCountPercent": "100"},
        ]),
    }
    def fake_get(url):
        for inst, body in fake_bodies.items():
            if f"/instruments/{inst}/" in url:
                return body
        raise AssertionError(f"unexpected URL {url}")
    monkeypatch.setattr(eng, "_http_get", fake_get)

    snap = eng.compute()
    assert set(snap.by_symbol) == {"USDJPY", "EURUSD"}
    assert snap.by_symbol["USDJPY"].bias == "short_squeeze"
    assert snap.by_symbol["EURUSD"].bias == "long_squeeze"
    assert snap.consecutive_failures == 0
    assert snap.last_error is None


def test_engine_total_failure_increments_counter(monkeypatch):
    eng = SentimentEngine(api_token="dummy", symbol_map={"USDJPY": "USD_JPY"})
    def boom(url):
        raise TimeoutError("network down")
    monkeypatch.setattr(eng, "_http_get", boom)

    snap1 = eng.compute()
    snap2 = eng.compute()
    assert snap1.by_symbol == {} and snap2.by_symbol == {}
    assert snap1.consecutive_failures == 1
    assert snap2.consecutive_failures == 2
    assert "USDJPY" in (snap2.last_error or "")


def test_engine_partial_failure_does_not_increment(monkeypatch):
    eng = SentimentEngine(api_token="dummy",
                          symbol_map={"USDJPY": "USD_JPY", "EURUSD": "EUR_USD"})
    body_usdjpy = _make_book(150.0, [
        {"price": "149.0", "longCountPercent": "100", "shortCountPercent": "0"},
        {"price": "151.0", "longCountPercent": "0",   "shortCountPercent": "100"},
    ])
    def flaky(url):
        if "USD_JPY" in url:
            return body_usdjpy
        raise TimeoutError("EUR_USD down")
    monkeypatch.setattr(eng, "_http_get", flaky)

    snap = eng.compute()
    assert "USDJPY" in snap.by_symbol
    assert "EURUSD" not in snap.by_symbol
    assert snap.consecutive_failures == 0
    assert "EURUSD" in (snap.last_error or "")
