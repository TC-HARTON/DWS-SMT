"""committed data/ema_disparity_bands.json のスキーマ検証。"""

from __future__ import annotations

import json

import config


def test_committed_bands_artifact_schema():
    path = config.PROJECT_ROOT / "data" / "ema_disparity_bands.json"
    assert path.exists(), "run scripts/gen_ema_disparity_bands.py first"
    doc = json.loads(path.read_text(encoding="utf-8"))
    modes = doc["modes"]
    assert set(modes) == {"M15", "H1"}
    expected = {"M15": (20, 80, 320), "H1": (20, 80, 480)}
    for name, periods in expected.items():
        m = modes[name]
        assert tuple(m["periods"]) == periods
        bands = m["bands"]
        assert set(bands) == {f"ema{p}" for p in periods}
        for key in bands:
            for side in ("pos", "neg"):
                s = bands[key][side]
                assert set(s) == {"p90", "p95", "p99", "max", "n"}
                assert s["p90"] <= s["p95"] <= s["p99"] <= s["max"]
                assert s["n"] > 0
