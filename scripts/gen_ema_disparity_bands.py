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

DEFAULT_CSV = config.PROJECT_ROOT / "XAUUSD_15 Mins_Bid_2010.01.01_2025.12.31.csv"
OUT_PATH = config.PROJECT_ROOT / "data" / "ema_disparity_bands.json"


def main(argv: list[str]) -> int:
    csv_path = Path(argv[1]) if len(argv) > 1 else DEFAULT_CSV
    if not csv_path.exists():
        print("ERROR: CSV not found: %s" % csv_path)
        return 1
    closes = read_dukascopy_closes(csv_path)
    bands = compute_bands(closes, periods=config.EMA_STACK_PERIODS)
    doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": csv_path.name,
        "tf": config.EMA_STACK_TF,
        "periods": list(config.EMA_STACK_PERIODS),
        "bands": bands,
    }
    OUT_PATH.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print("OK wrote %s (bars=%d)" % (OUT_PATH, closes.size))
    for key, b in bands.items():
        print("  %-6s pos p95=%.3f p99=%.3f max=%.3f n=%d | "
              "neg p95=%.3f p99=%.3f max=%.3f n=%d" % (
                  key,
                  b["pos"]["p95"], b["pos"]["p99"], b["pos"]["max"], b["pos"]["n"],
                  b["neg"]["p95"], b["neg"]["p99"], b["neg"]["max"], b["neg"]["n"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
