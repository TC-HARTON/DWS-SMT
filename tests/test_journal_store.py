"""Tests for the per-broker discretionary-trade journal store."""

from __future__ import annotations

import json

import pytest

from analyzer import journal_store as js


@pytest.fixture
def journal_dir(tmp_path, monkeypatch):
    """Point the journal at an isolated temp dir."""
    monkeypatch.setattr(js, "JOURNAL_DIR", tmp_path)
    return tmp_path


def _entry(ts: int, side: str = "BUY") -> dict:
    return {"ts": ts, "symbol": "XAUUSD", "side": side, "lots": 0.05,
            "sl": None, "tp": None, "ticket": ts, "price": 3400.0,
            "ctx": {"M15": {"ae": True, "adx": 41.2}}}


def test_append_then_load_newest_first(journal_dir):
    server = "ICMarketsSC-MT5-3"
    js.append(server, _entry(1000))
    js.append(server, _entry(3000))
    js.append(server, _entry(2000))
    rows = js.load_recent(server)
    assert [r["ts"] for r in rows] == [3000, 2000, 1000]   # newest first
    # The file lives under a slug of the server name.
    assert js.store_path(server).exists()
    assert js.store_path(server).parent.name == "ICMarketsSC-MT5-3"


def test_load_recent_missing_returns_empty(journal_dir):
    assert js.load_recent("NoSuchBroker") == []


def test_load_recent_respects_limit(journal_dir):
    server = "Demo"
    for i in range(10):
        js.append(server, _entry(i))
    rows = js.load_recent(server, limit=3)
    assert len(rows) == 3
    assert [r["ts"] for r in rows] == [9, 8, 7]


def test_corrupt_line_is_skipped(journal_dir):
    server = "Demo"
    js.append(server, _entry(1))
    # Inject a non-JSON line; load must skip it rather than abort.
    with js.store_path(server).open("a", encoding="utf-8") as fh:
        fh.write("{ not json\n")
    js.append(server, _entry(2))
    rows = js.load_recent(server)
    assert [r["ts"] for r in rows] == [2, 1]


def test_none_server_slugs_to_unknown(journal_dir):
    js.append(None, _entry(5))
    assert js.store_path(None).parent.name == "unknown"
    assert [r["ts"] for r in js.load_recent(None)] == [5]


def test_slug_cannot_traverse_parent_dir(journal_dir):
    # A pathological server name must never resolve outside the journal dir.
    for evil in ("..", "../..", "../secret"):
        p = js.store_path(evil).resolve()
        assert journal_dir.resolve() in p.parents, f"{evil!r} escaped: {p}"
        assert ".." not in js.store_path(evil).parent.name


def test_unicode_and_roundtrip_fidelity(journal_dir):
    server = "Brök/er:MT5"          # slug must sanitise the unsafe chars
    entry = _entry(7, side="SELL")
    entry["note"] = "日本語メモ"
    js.append(server, entry)
    [row] = js.load_recent(server)
    assert row == entry            # exact JSON round-trip
    # Path slug strips '/' and ':' so it is a single safe directory segment.
    assert "/" not in js.store_path(server).parent.name
    assert ":" not in js.store_path(server).parent.name
    # And the stored bytes are real UTF-8 (ensure_ascii=False).
    assert "日本語メモ" in js.store_path(server).read_text(encoding="utf-8")
    assert json.loads(js.store_path(server).read_text(encoding="utf-8").strip())["note"] == "日本語メモ"


def test_env_from_snapshot_reads_defensively():
    from dashboard.lite_server import _env_from_snapshot
    # Real field names verified from the dataclasses:
    #   DxySnapshot.price (level), DxySnapshot.change (change)
    #   CotSnapshot.pctile_1y (1-year percentile)
    #   RealYieldSnapshot.value (level), RealYieldSnapshot.change_1d (1-day change)
    snap = {
        "dxy": type("D", (), {"price": 99.4, "change": 0.3})(),
        "cot": type("C", (), {"pctile_1y": 5.0})(),
        "real_yield": type("R", (), {"value": 2.1, "change_1d": -0.02})(),
    }
    env = _env_from_snapshot(snap)
    assert env["dxy_level"] == 99.4
    assert env["dxy_change"] == 0.3
    assert env["cot_pctile"] == 5.0
    assert env["real_yield_level"] == 2.1
    assert env["real_yield_change"] == -0.02
    # 欠落だらけでも例外を出さず空 dict
    assert _env_from_snapshot({}) == {}
