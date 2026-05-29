"""Tests for the persistent live trigger-history store."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from analyzer import trigger_store as ts
from analyzer.signal_validator import RecentTrigger


@pytest.fixture
def store_dir(tmp_path, monkeypatch):
    """Point the store at an isolated temp dir and clear module caches."""
    monkeypatch.setattr(ts.config, "LIVE_TRIGGER_DIR", tmp_path)
    ts._seen.clear()
    ts._locks.clear()
    ts._by_year_cache.clear()
    return tmp_path


def _rt(entry_ms: int, direction: int, net: float, is_open: bool = False) -> RecentTrigger:
    return RecentTrigger(entry_ms=entry_ms, direction=direction,
                         net_pts=net, is_open=is_open)


def test_append_skips_open_and_dedups(store_dir):
    server = "ICMarketsSC-MT5-3"
    trigs = [_rt(1000, 1, 5.0), _rt(2000, -1, -3.0), _rt(3000, 1, 7.0, is_open=True)]

    # Open trigger is skipped; the two closed ones are written.
    assert ts.append_closed(server, "XAUUSD", "M15", trigs) == 2
    # Re-appending the same window adds nothing (dedup by entry_ms).
    assert ts.append_closed(server, "XAUUSD", "M15", trigs) == 0
    # Once the previously-open trigger closes, it is appended.
    assert ts.append_closed(server, "XAUUSD", "M15", [_rt(3000, 1, 7.0)]) == 1

    path = ts.store_path(server, "XAUUSD", "M15")
    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(rows) == 3
    assert {r["t"] for r in rows} == {1000, 2000, 3000}


def test_dedup_survives_cache_reload(store_dir):
    """Dedup must work even after the in-memory seen-set is dropped (restart)."""
    server = "BrokerX"
    ts.append_closed(server, "EURUSD", "H1", [_rt(111, 1, 2.0)])
    ts._seen.clear()  # simulate a fresh process — seen-set re-read from disk
    assert ts.append_closed(server, "EURUSD", "H1", [_rt(111, 1, 2.0)]) == 0


def test_load_by_year_stats_and_order(store_dir):
    server = "BrokerX"
    jan = int(pd.Timestamp("2026-01-15 10:00", tz="Asia/Tokyo").timestamp() * 1000)
    feb1 = int(pd.Timestamp("2026-02-20 10:00", tz="Asia/Tokyo").timestamp() * 1000)
    feb2 = feb1 + 60_000
    ts.append_closed(server, "EURUSD", "H1",
                     [_rt(jan, 1, 10.0), _rt(feb1, -1, -4.0), _rt(feb2, 1, 6.0)])

    by_year = ts.load_by_year(server, "EURUSD", "H1")["by_year"]
    assert set(by_year) == {"2026"}
    rec = by_year["2026"]
    assert (rec["n"], rec["wins"], rec["losses"]) == (3, 2, 1)
    assert rec["cum_pts"] == pytest.approx(12.0)
    assert rec["gross_win"] == pytest.approx(16.0)
    assert rec["gross_loss"] == pytest.approx(4.0)
    assert rec["win_rate"] == pytest.approx(2 / 3, abs=1e-3)
    # Trades newest-first, capped at 30.
    assert [t["t"] for t in rec["trades"]] == [feb2, feb1, jan]


def test_jst_year_bucketing(store_dir):
    """A 2025-12-31 23:00 UTC trigger is 2026-01-01 08:00 JST → bucket 2026."""
    server = "BrokerX"
    utc_nye = int(pd.Timestamp("2025-12-31 23:00", tz="UTC").timestamp() * 1000)
    ts.append_closed(server, "XAUUSD", "H4", [_rt(utc_nye, 1, 1.0)])
    by_year = ts.load_by_year(server, "XAUUSD", "H4")["by_year"]
    assert set(by_year) == {"2026"}


def test_broker_isolation_and_slug(store_dir):
    a = ts.store_path("Broker A/X", "XAUUSD", "M15")
    b = ts.store_path("Broker B", "XAUUSD", "M15")
    assert a.parent != b.parent              # different brokers → different dirs
    assert "/" not in a.parent.name          # path-unsafe chars sanitised
    assert " " not in a.parent.name


def test_load_empty_when_no_store(store_dir):
    assert ts.load_by_year("Nobody", "XAUUSD", "M15") == {"by_year": {}}


def test_load_by_year_cache_invalidates_on_append(store_dir):
    """The memoised load result refreshes when the store grows, and is never
    the cached object itself (so a caller cannot mutate the cache)."""
    server = "BrokerX"
    jan = int(pd.Timestamp("2026-01-15 10:00", tz="Asia/Tokyo").timestamp() * 1000)
    ts.append_closed(server, "EURUSD", "H1", [_rt(jan, 1, 10.0)])

    first = ts.load_by_year(server, "EURUSD", "H1")
    assert first["by_year"]["2026"]["n"] == 1

    # Two reads with no change between are equal but independent objects.
    again = ts.load_by_year(server, "EURUSD", "H1")
    assert again == first and again is not first
    again["by_year"]["2026"]["n"] = 999                      # mutate the copy …
    assert ts.load_by_year(server, "EURUSD", "H1")["by_year"]["2026"]["n"] == 1  # … no leak

    # A fresh append grows the file → cache self-invalidates → reflects it.
    feb = int(pd.Timestamp("2026-02-20 10:00", tz="Asia/Tokyo").timestamp() * 1000)
    ts.append_closed(server, "EURUSD", "H1", [_rt(feb, -1, -4.0)])
    after = ts.load_by_year(server, "EURUSD", "H1")
    assert after["by_year"]["2026"]["n"] == 2
