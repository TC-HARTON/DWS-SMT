"""committed data/ema_disparity_bands.json のスキーマ検証。"""

from __future__ import annotations

import json

import config


def test_committed_bands_artifact_schema():
    path = config.PROJECT_ROOT / "data" / "ema_disparity_bands.json"
    assert path.exists(), "run scripts/gen_ema_disparity_bands.py first"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["tf"] == config.EMA_STACK_TF
    assert doc["periods"] == list(config.EMA_STACK_PERIODS)
    bands = doc["bands"]
    expected_keys = {f"ema{p}" for p in config.EMA_STACK_PERIODS}
    assert set(bands) == expected_keys
    for key in expected_keys:
        for side in ("pos", "neg"):
            s = bands[key][side]
            assert set(s) == {"p90", "p95", "p99", "max", "n"}
            assert s["p90"] <= s["p95"] <= s["p99"] <= s["max"]
            assert s["n"] > 0
