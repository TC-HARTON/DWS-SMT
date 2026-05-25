"""Generate the OOS-baseline JSON the live dashboard displays beside its
short-window validation tier.

Runs the production DWS-SMT trigger + signal_validator.evaluate_trades on
16 y of Dukascopy data per symbol/base TF with swap costs applied (matches
``_backtest_all_yearly_swap.py``), takes the *overall* ValidationCore for
each (symbol, base_tf), and writes the result to
``data/oos_baseline.json`` for the lite dashboard to surface.

This is a one-shot offline script — the user runs it once per
data-refresh cycle. The dashboard loads the resulting JSON at startup
and never recomputes during the live session.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from analyzer import dws_smt, indicators  # noqa: E402
from analyzer.signal_validator import evaluate_trades  # noqa: E402

# Re-use loaders + pairer from the yearly script to keep behaviour identical.
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import _backtest_all_yearly_swap as yearly  # type: ignore  # noqa: E402

OUT_FILE = PROJECT_ROOT / "data" / "oos_baseline.json"


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("gen_oos_baseline")

    rates = yearly.load_rates()
    symbols = [s.base for s in config.SYMBOLS]
    log.info("Computing 16y OOS baseline for %d symbols × 3 base TFs", len(symbols))

    t0 = time.perf_counter()
    by_symbol: dict[str, dict[str, dict]] = {}
    n_cells = 0
    earliest = None
    latest = None
    for sym in symbols:
        if sym not in yearly.POINT_BY_SYMBOL:
            continue
        try:
            cells = yearly.run_symbol(sym, rates)
        except FileNotFoundError as e:
            log.warning("%s: CSV missing — skipping (%s)", sym, e)
            continue
        per_tf: dict[str, dict] = {}
        for cell in cells:
            c = cell.overall
            pf = (None if c.profit_factor == float("inf")
                  else round(c.profit_factor, 4))
            per_tf[cell.base_tf] = {
                "n_trades": int(c.n_trades),
                "win_rate": round(c.win_rate, 4),
                "ci_low":   round(c.ci_low, 4),
                "ci_high":  round(c.ci_high, 4),
                "profit_factor": pf,
                "expectancy": round(c.expectancy, 2),
                "max_drawdown": round(c.max_drawdown, 2),
                "tier": c.tier,
            }
            n_cells += 1
        if per_tf:
            by_symbol[sym] = per_tf

    elapsed = time.perf_counter() - t0
    payload = {
        "schema_version": 1,
        "generated_at": time.time(),
        "generated_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "Dukascopy CSV 2010-01-01 → 2025-12-31 (W1 starts 2009-12-28)",
        "compute_method": "production dws_smt.compute_symbol + signal_validator.evaluate_trades",
        "signal_out_only": True,
        "swap_costs_applied": True,
        "swap_source": "FRED IRSTCI01* policy-rate proxies (monthly, fwd-filled)",
        "window_years_max": 16,
        "skipped_warmup_year": 2010,
        "by_symbol": by_symbol,
    }

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    log.info("Wrote %d cells (%d symbols) → %s in %.1fs",
             n_cells, len(by_symbol), OUT_FILE, elapsed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
