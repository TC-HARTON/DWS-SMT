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


def test_load_by_year_includes_hourly_breakdown(store_dir):
    """Each year ships a 24-bucket JST-hour breakdown for the time-of-day
    heatmap (16Y baseline + live merge)."""
    server = "BrokerX"
    h10 = int(pd.Timestamp("2026-02-10 10:30", tz="Asia/Tokyo").timestamp() * 1000)
    h10b = int(pd.Timestamp("2026-02-11 10:05", tz="Asia/Tokyo").timestamp() * 1000)
    ts.append_closed(server, "XAUUSD", "M15",
                     [_rt(h10, 1, 5.0), _rt(h10b, -1, -3.0)])
    hourly = ts.load_by_year(server, "XAUUSD", "M15")["by_year"]["2026"]["hourly"]
    assert len(hourly) == 24
    b10 = next(b for b in hourly if b["hour"] == 10)
    assert (b10["n"], b10["wins"]) == (2, 1)          # 2 trades @10時JST, 1 win
    assert sum(b["n"] for b in hourly) == 2           # no leakage to other hours


def test_load_by_year_includes_monthly_breakdown(store_dir):
    """Each year ships a per-month aggregate (JST) so the dashboard can render the
    monthly-returns calendar over the COMPLETE record — not a truncated trade
    list. Only months with trades appear; stats mirror the yearly aggregate."""
    server = "BrokerX"
    jan = int(pd.Timestamp("2026-01-15 10:00", tz="Asia/Tokyo").timestamp() * 1000)
    mar1 = int(pd.Timestamp("2026-03-05 10:00", tz="Asia/Tokyo").timestamp() * 1000)
    mar2 = int(pd.Timestamp("2026-03-20 10:00", tz="Asia/Tokyo").timestamp() * 1000)
    ts.append_closed(server, "XAUUSD", "M15",
                     [_rt(jan, 1, 10.0), _rt(mar1, -1, -4.0), _rt(mar2, 1, 6.0)])

    months = ts.load_by_year(server, "XAUUSD", "M15")["by_year"]["2026"]["months"]
    assert set(months) == {"1", "3"}                 # only months with trades
    assert (months["1"]["n"], months["1"]["cum_pts"]) == (1, pytest.approx(10.0))
    assert (months["3"]["n"], months["3"]["wins"]) == (2, 1)
    assert months["3"]["cum_pts"] == pytest.approx(2.0)   # -4 + 6


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


def _rec(t: int, d: int, p: float) -> dict:
    return {"t": t, "d": d, "p": p}


def test_scan_corruption_flags_restamp_triple():
    """The [0,+4h,+5h] offset-bug fingerprint (one trade under three offsets)
    must be detected."""
    base = 1_700_000_000_000
    recs = [_rec(base, 1, 6784.0),
            _rec(base + 4 * 3_600_000, 1, 6784.0),
            _rec(base + 5 * 3_600_000, 1, 6784.0)]
    flags = ts.scan_corruption(recs)
    assert flags["tight_triples"] >= 1
    assert flags["exact_t_dups"] == 0


def test_scan_corruption_flags_exact_duplicate():
    recs = [_rec(1000, 1, 5.0), _rec(1000, 1, 5.0)]
    assert ts.scan_corruption(recs)["exact_t_dups"] == 1


def test_scan_corruption_ignores_coincidental_distinct_trades():
    """Two genuinely distinct trades sharing a rounded net_pts — even a whole
    number of hours apart — are NOT corruption (deleting them would falsify the
    record)."""
    base = 1_700_000_000_000
    recs = [
        _rec(base, 1, -35.0),
        _rec(base + 5 * 3_600_000, 1, -35.0),     # coincidental PAIR (<=6h) — only 2
        _rec(base + 40 * 3_600_000, -1, -35.0),   # far away, different direction
    ]
    flags = ts.scan_corruption(recs)
    assert flags == {"exact_t_dups": 0, "tight_triples": 0}


def test_real_store_has_no_restamp_corruption():
    """Invariant over the ACTUAL on-disk store: no file may carry the re-stamp
    fingerprint. This is the continuous, human-independent proof that the offset
    fix holds — if the store ever rots again, this test fails. Skips cleanly when
    no store exists (e.g. CI without data)."""
    root = ts.config.LIVE_TRIGGER_DIR
    if not root.exists():
        pytest.skip("no live trigger store on disk")
    files = list(root.rglob("*.jsonl"))
    if not files:
        pytest.skip("live trigger store is empty")
    dirty = []
    for path in files:
        recs = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                recs.append({"t": int(r["t"]), "d": int(r["d"]), "p": float(r["p"])})
            except (ValueError, KeyError, TypeError):
                continue
        flags = ts.scan_corruption(recs)
        if flags["exact_t_dups"] or flags["tight_triples"]:
            dirty.append((str(path.relative_to(root)), flags))
    assert not dirty, f"re-stamp corruption present: {dirty}"


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
