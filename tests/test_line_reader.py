"""Unit tests for analyzer.line_reader (SPEC §9)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from analyzer.line_reader import (
    LinesState,
    LinesWatcher,
    _classify,
    parse_lines_payload,
)
from analyzer.structure_types import LevelKind, LevelSource


# --------------------------------------------------------------------------- #
# Name classifier (SPEC §9.3)
# --------------------------------------------------------------------------- #

def test_classify_resistance_with_strong_modifier():
    cat, importance, tf = _classify("R1_strong_D1", LevelKind.HORIZONTAL)
    assert cat == "resistance"
    assert importance == 3
    assert tf == "D1"


def test_classify_trend_line_up_with_tf():
    cat, importance, tf = _classify("TL_up_major_H4", LevelKind.TREND_LINE)
    assert cat == "trend_up"
    assert importance == 2
    assert tf == "H4"


def test_classify_zone_supply():
    cat, _, _ = _classify("zone_supply_top", LevelKind.RECTANGLE)
    assert cat == "supply_zone"


def test_classify_fallback_for_unknown_name():
    cat, importance, tf = _classify("MyRandomName", LevelKind.HORIZONTAL)
    assert cat == "other"
    assert importance == 1
    assert tf is None


def test_classify_uses_kind_for_fibonacci_when_prefix_missing():
    cat, _, _ = _classify("fib_2025_swing", LevelKind.FIBONACCI)
    assert cat == "fibonacci"


# --------------------------------------------------------------------------- #
# parse_lines_payload (full SPEC §9.2 schema)
# --------------------------------------------------------------------------- #

def test_parse_horizontal_and_trendline():
    payload = {
        "symbol": "XAUUSD",
        "updated_at": "2026-05-18T23:45:12+09:00",
        "lines": {
            "horizontal": [
                {"name": "R1_strong_D1", "price": 3265.00, "color": "#FF0000"},
            ],
            "trendlines": [
                {
                    "name": "TL_up_D1",
                    "point1": ["2026-05-10T00:00", 3180.50],
                    "point2": ["2026-05-15T00:00", 3220.30],
                    "ray_right": True,
                    "current_value": 3242.20,
                    "slope_per_day": 7.96,
                    "color": "#00FF00",
                },
            ],
        },
    }
    levels = parse_lines_payload(payload)
    assert len(levels) == 2
    h, t = levels
    assert h.kind == LevelKind.HORIZONTAL
    assert h.price == 3265.00
    assert h.importance == 3
    assert h.source == LevelSource.EA_USER
    assert t.kind == LevelKind.TREND_LINE
    assert t.price == 3242.20
    assert t.meta["ray_right"] is True
    assert t.meta["slope_per_day"] == 7.96


def test_parse_rectangle_explodes_into_two_edges():
    payload = {
        "symbol": "XAUUSD",
        "lines": {
            "rectangles": [
                {"name": "zone_supply_a", "time1": "x", "time2": "y",
                 "price_low": 100.0, "price_high": 105.0, "color": "#FF00FF"},
            ],
        },
    }
    levels = parse_lines_payload(payload)
    assert len(levels) == 2
    prices = sorted(l.price for l in levels)
    assert prices == [100.0, 105.0]
    assert all(l.kind == LevelKind.RECTANGLE for l in levels)


def test_parse_channel_yields_main_and_parallel():
    payload = {
        "symbol": "XAUUSD",
        "lines": {
            "channels": [
                {
                    "name": "TL_up_chan",
                    "main_point1": ["t1", 100],
                    "main_point2": ["t2", 110],
                    "parallel_anchor": ["t3", 95],
                    "ray_right": True,
                    "main_value": 112.0,
                    "parallel_value": 102.0,
                    "color": "#FFFFFF",
                },
            ],
        },
    }
    levels = parse_lines_payload(payload)
    kinds = sorted(l.kind for l in levels)
    assert LevelKind.CHANNEL_MAIN in kinds and LevelKind.CHANNEL_PARALLEL in kinds


def test_parse_fibonacci_emits_one_level_per_ratio():
    payload = {
        "symbol": "XAUUSD",
        "lines": {
            "fibonacci": [
                {
                    "name": "fib_a",
                    "point1": ["t1", 100],
                    "point2": ["t2", 200],
                    "levels": [
                        {"ratio": 0.382, "label": "0.382", "price": 138.2},
                        {"ratio": 0.618, "label": "0.618", "price": 161.8},
                    ],
                },
            ],
        },
    }
    levels = parse_lines_payload(payload)
    prices = sorted(l.price for l in levels)
    assert prices == [pytest.approx(138.2), pytest.approx(161.8)]
    assert all(l.kind == LevelKind.FIBONACCI for l in levels)


def test_parse_text_note():
    payload = {
        "symbol": "XAUUSD",
        "lines": {
            "texts": [
                {"name": "annot_a", "time": "t1", "price": 100.0, "text": "key zone"},
            ],
        },
    }
    levels = parse_lines_payload(payload)
    assert len(levels) == 1
    assert levels[0].kind == LevelKind.TEXT_NOTE
    assert levels[0].meta["text"] == "key zone"


def test_parse_skips_malformed_entries_without_raising():
    payload = {
        "symbol": "XAUUSD",
        "lines": {
            "horizontal": [
                {"name": "R1", "price": 100.0},
                {"name": "broken"},   # missing price field
            ],
        },
    }
    levels = parse_lines_payload(payload)
    assert len(levels) == 1
    assert levels[0].price == 100.0


def test_parse_returns_empty_when_symbol_missing():
    assert parse_lines_payload({"lines": {}}) == []


# --------------------------------------------------------------------------- #
# LinesState
# --------------------------------------------------------------------------- #

def test_lines_state_round_trip():
    s = LinesState()
    levels_in = parse_lines_payload({
        "symbol": "XAUUSD",
        "lines": {"horizontal": [{"name": "R1", "price": 100.0}]},
    })
    s.update_symbol("XAUUSD", levels_in)
    assert s.levels_for("XAUUSD") == levels_in
    snap = s.snapshot()
    assert snap["XAUUSD"][0].price == 100.0
    assert s.updated_at("XAUUSD") is not None
    assert s.updated_at("EURUSD") is None


# --------------------------------------------------------------------------- #
# LinesWatcher integration with the filesystem (via tmp_path)
# --------------------------------------------------------------------------- #

def test_watcher_reload_existing_picks_up_initial_files(tmp_path: Path):
    path = tmp_path / "lines_XAUUSD.json"
    path.write_text(json.dumps({
        "symbol": "XAUUSD",
        "lines": {"horizontal": [{"name": "R1", "price": 4500.0}]},
    }))
    state = LinesState()
    w = LinesWatcher(directory=tmp_path, state=state)
    w.reload_existing()
    assert state.levels_for("XAUUSD")[0].price == 4500.0


def test_watcher_skips_files_that_dont_match_prefix(tmp_path: Path):
    path = tmp_path / "other_file.json"
    path.write_text("{}")
    state = LinesState()
    w = LinesWatcher(directory=tmp_path, state=state)
    w.reload_existing()
    assert state.snapshot() == {}


def test_watcher_handles_mid_write_empty_file(tmp_path: Path):
    """An empty file mid-EA-write must not raise."""
    path = tmp_path / "lines_XAUUSD.json"
    path.write_text("")
    state = LinesState()
    w = LinesWatcher(directory=tmp_path, state=state)
    w.reload_existing()
    assert state.snapshot() == {}


def test_watcher_handles_corrupted_json(tmp_path: Path):
    path = tmp_path / "lines_XAUUSD.json"
    path.write_text("{not json")
    state = LinesState()
    w = LinesWatcher(directory=tmp_path, state=state)
    w.reload_existing()
    assert state.snapshot() == {}


def test_watcher_observes_new_file(tmp_path: Path):
    state = LinesState()
    w = LinesWatcher(directory=tmp_path, state=state)
    w.start()
    try:
        path = tmp_path / "lines_XAUUSD.json"
        path.write_text(json.dumps({
            "symbol": "XAUUSD",
            "lines": {"horizontal": [{"name": "R1", "price": 4500.0}]},
        }))
        # Watchdog should pick up the event within a second.
        deadline = time.time() + 3.0
        while time.time() < deadline and not state.levels_for("XAUUSD"):
            time.sleep(0.05)
        levels = state.levels_for("XAUUSD")
        assert levels and levels[0].price == 4500.0
    finally:
        w.stop()
