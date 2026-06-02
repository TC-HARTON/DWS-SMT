"""LEGACY / schema_version=1 OOS-baseline writer — superseded.

The canonical baseline writer is now ``scripts/_oos_xauusd_16y.py`` which
emits ``data/oos_baseline.json`` with ``schema_version=2`` including the
rich keys the live dashboard depends on:

* ``bootstrap_ci`` — moving-block bootstrap WR confidence interval
* ``period_split`` — early vs late half Welch-t / WR-z verdict
* ``year_breakdown`` — per-year n / WR / cum-pts arrays
* ``hourly_winrate`` — 24-bucket JST hourly win-rate (heatmap source)
* ``trigger_history`` — per-year + per-month aggregate (calendar source)

This older script wrote only the flat back-compat shape (``n_trades``,
``win_rate``, ``ci_low``, ``ci_high``, ``profit_factor``, ``expectancy``,
``max_drawdown``, ``tier``) with ``schema_version=1``. Running it on top
of a schema_version=2 file would SILENTLY strip every rich key the
dashboard reads — the hourly heatmap, trigger calendar and regime gate
would all blank out with no error.

A version guard at the top of ``main()`` refuses to overwrite a higher
schema_version unless ``FORCE=1`` is set, so this script can stay around
as a legacy reference but cannot break the live data by accident.
"""

from __future__ import annotations

import json
import logging
import os
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


_OWN_SCHEMA_VERSION = 1


def _refuse_schema_downgrade(log: logging.Logger) -> int | None:
    """Refuse to overwrite a higher-schema baseline unless ``FORCE=1`` is set.

    Returns ``None`` to proceed, or an exit code to abort. Reads the existing
    baseline's ``schema_version`` and compares against this script's own
    version (1). The canonical writer is ``_oos_xauusd_16y.py`` which emits
    version 2 with rich keys the dashboard depends on — silently downgrading
    them would blank the hourly heatmap, trigger calendar and regime gate.
    """
    if not OUT_FILE.exists():
        return None
    try:
        existing = json.loads(OUT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("existing %s unreadable (%s) — proceeding with overwrite",
                    OUT_FILE.name, exc)
        return None
    have = int(existing.get("schema_version") or 0)
    if have <= _OWN_SCHEMA_VERSION:
        return None
    if os.environ.get("FORCE") == "1":
        log.warning(
            "FORCE=1 — overwriting schema_version=%d with schema_version=%d "
            "(rich keys will be lost)", have, _OWN_SCHEMA_VERSION,
        )
        return None
    log.error(
        "REFUSING to overwrite %s: existing schema_version=%d is newer than "
        "this script's schema_version=%d. The canonical writer is "
        "scripts/_oos_xauusd_16y.py. Set FORCE=1 to override (you will lose "
        "bootstrap_ci, period_split, year_breakdown, hourly_winrate, "
        "trigger_history).",
        OUT_FILE.name, have, _OWN_SCHEMA_VERSION,
    )
    return 2


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("gen_oos_baseline")

    abort = _refuse_schema_downgrade(log)
    if abort is not None:
        return abort

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
        "schema_version": _OWN_SCHEMA_VERSION,
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
