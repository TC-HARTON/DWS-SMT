"""disparity_bands: 乖離率パーセンタイル帯の算出 + ロード + CSV読み。"""

from __future__ import annotations

import json

import numpy as np

from analyzer.disparity_bands import (
    compute_bands,
    load_bands,
    read_dukascopy_closes,
)


def test_compute_bands_structure_and_ordering():
    rng = np.random.default_rng(0)
    closes = 1000 + np.cumsum(rng.normal(0, 1.0, 5000))
    bands = compute_bands(closes, periods=(20, 80, 320))
    assert set(bands) == {"ema20", "ema80", "ema320"}
    for key in bands:
        for side in ("pos", "neg"):
            s = bands[key][side]
            assert set(s) == {"p90", "p95", "p99", "max", "n"}
            assert s["p90"] <= s["p95"] <= s["p99"] <= s["max"]
            assert s["n"] >= 0


def test_compute_bands_pos_neg_split():
    closes = np.concatenate([
        np.full(400, 100.0),
        np.linspace(100, 130, 200),   # rally -> price above EMA (pos)
        np.linspace(130, 90, 300),    # drop  -> price below EMA (neg)
    ])
    bands = compute_bands(closes, periods=(20, 80, 320))
    assert bands["ema20"]["pos"]["n"] > 0
    assert bands["ema20"]["neg"]["n"] > 0


def test_load_bands_missing(tmp_path):
    assert load_bands("M15", path=tmp_path / "nope.json") is None


def test_load_bands_valid(tmp_path):
    p = tmp_path / "b.json"
    p.write_text(json.dumps({"modes": {
        "M15": {"bands": {"ema20": {"pos": {}, "neg": {}}}},
        "H1":  {"bands": {"ema480": {"pos": {}, "neg": {}}}},
    }}), encoding="utf-8")
    assert load_bands("M15", path=p) == {"ema20": {"pos": {}, "neg": {}}}
    assert load_bands("H1", path=p) == {"ema480": {"pos": {}, "neg": {}}}


def test_load_bands_unknown_mode(tmp_path):
    p = tmp_path / "b.json"
    p.write_text(json.dumps({"modes": {"M15": {"bands": {}}}}), encoding="utf-8")
    assert load_bands("H1", path=p) is None


def test_load_bands_malformed(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    assert load_bands("M15", path=p) is None


def test_read_dukascopy_closes(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text(
        "Time (EET),Open,High,Low,Close,Volume \n"
        "2010.01.01 00:00:00,1.0,2.0,0.5,1.5,10\n"
        "2010.01.01 00:15:00,1.5,2.5,1.0,2.0,11\n",
        encoding="utf-8",
    )
    out = read_dukascopy_closes(p)
    assert list(out) == [1.5, 2.0]


def test_serialize_ema_stack_includes_bands():
    from analyzer.ema_stack import EmaStackSnapshot
    from dashboard.serialize import serialize_ema_stack

    snap = EmaStackSnapshot(
        symbol="XAUUSD", periods=(20, 80, 320),
        price=4000.0, ema_fast=4000.0, ema_mid=3990.0, ema_center=3950.0,
        times_ms=(1, 2), dev_price=(0.1, 0.2), dev_fast=(0.0, 0.1),
        dev_mid=(0.0, 0.0), as_of=1.0, stale=False,
        bands={"ema20": {"pos": {}, "neg": {}}},
    )
    out = serialize_ema_stack(snap)
    assert out["bands"] == {"ema20": {"pos": {}, "neg": {}}}
    assert serialize_ema_stack(None) is None
