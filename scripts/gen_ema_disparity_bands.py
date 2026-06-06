"""Generate data/ema_disparity_bands.json from the 16Y Dukascopy M15 Bid CSV.

Offline tool (run on the machine that holds the bulk CSV). Reads the close
series, computes per-EMA per-side disparity percentile bands
(analyzer.disparity_bands), and writes the small committed JSON the server
serves to the oscillator readout. ASCII-only output (Windows cp932).

Usage:
    python scripts/gen_ema_disparity_bands.py [csv_path]
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from analyzer.disparity_bands import compute_bands, read_dukascopy_closes

OUT_PATH = config.PROJECT_ROOT / "data" / "ema_disparity_bands.json"

# 16Y Dukascopy Bid CSV per mode (same format, different timeframe).
_SOURCES = {
    "M15": config.PROJECT_ROOT / "XAUUSD_15 Mins_Bid_2010.01.01_2025.12.31.csv",
    "H1":  config.PROJECT_ROOT / "XAUUSD_Hourly_Bid_2010.01.01_2025.12.31.csv",
}


def main(argv: list[str]) -> int:
    modes_out = {}
    for spec in config.EMA_STACK_MODES:
        csv_path = _SOURCES[spec.name]
        if not csv_path.exists():
            print("ERROR: CSV not found: %s" % csv_path)
            return 1
        closes = read_dukascopy_closes(csv_path)
        bands = compute_bands(closes, periods=spec.periods)
        modes_out[spec.name] = {
            "tf": spec.tf, "periods": list(spec.periods),
            "source": csv_path.name, "bands": bands,
        }
        print("OK %s (bars=%d): " % (spec.name, closes.size) + ", ".join(
            "%s pos=%.2f neg=%.2f(p99)" % (k, b["pos"]["p99"], b["neg"]["p99"])
            for k, b in bands.items()))
    doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "modes": modes_out,
    }
    OUT_PATH.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print("OK wrote %s" % OUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
