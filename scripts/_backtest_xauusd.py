"""Offline XAUUSD backtest — runs the live ``SignalValidator`` on Dukascopy
CSV history (W1/D1/H4/H1/M15, Bid+Ask, 2010–2025).

The point of this script is **strict logic compliance**: it does NOT
re-implement the DWS-SMT trigger or the validation metrics. It loads CSVs into
MT5-compatible OHLC DataFrames (UTC index, ``open/high/low/close/spread``),
wraps them in a connector that satisfies :class:`SignalValidator`'s contract,
and lets the production :func:`dws_smt.compute_symbol` and
:func:`signal_validator.evaluate_trades` do the work. Results are therefore
directly comparable to the dashboard's live "信頼/要注意/データ不足" tier — only
the sample size differs (16 years instead of the live 2 000-bar window).

Disclosed approximations (also printed by the script):

* **BIAS series = zeros.** The validator does not pass ``bias_contrib``; the
  per-bar BIAS column on the emitted window is therefore all zeros. This has
  NO effect on the trades or stats (BIAS is not used in trigger / pairing /
  scoring), only on a display-only field we do not consume here.
* **Macro / real-yield / calendar filters are disabled.** We have no historical
  policy-rate or real-yield state for 2010-2025 in this run, so
  ``ValidationStats.macro_filtered`` equals ``raw`` (the same convention the
  live validator currently uses).
* **Spread cost** is derived per-bar from ``ask_close − bid_close``. This is
  more faithful than the live system's snapshot-spread proxy.
* **EET is treated as fixed UTC+2** (no DST). Bar boundaries can sit one hour
  off the broker's local week half the year, but the indicator math is
  invariant to that shift.

Run from the project root::

    py scripts/_backtest_xauusd.py
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import pandas as pd

# Make the project root importable when this is run as a script.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from analyzer.signal_validator import SignalValidator  # noqa: E402


# --------------------------------------------------------------------------- #
# CSV layout
# --------------------------------------------------------------------------- #

# Dukascopy filename token for each TF label we use.
_TF_FILENAMES: dict[str, str] = {
    "W1":  "Weekly",
    "D1":  "Daily",
    "H4":  "4 Hours",
    "H1":  "Hourly",
    "M15": "15 Mins",
}

# The date range encoded in each filename (the file's own start/end markers).
_DATE_RANGE: dict[str, str] = {
    "W1":  "2009.12.28_2025.12.29",
    "D1":  "2010.01.01_2025.12.31",
    "H4":  "2010.01.01_2025.12.31",
    "H1":  "2010.01.01_2025.12.31",
    "M15": "2010.01.01_2025.12.31",
}

# MT5 point size for XAUUSD on Exness (3-digit pricing — verified in
# analyzer/mt5_connector.py:325 and against the broker's `symbol_info().point`).
XAUUSD_POINT: float = 0.001


# --------------------------------------------------------------------------- #
# CSV loader
# --------------------------------------------------------------------------- #

def _load_csv(tf: str, side: str) -> pd.DataFrame:
    """Load one Dukascopy CSV (``Bid`` or ``Ask``) for one timeframe.

    Returns OHLC + tick-volume indexed by UTC time (EET → UTC = -2 h).
    """
    fname = f"XAUUSD_{_TF_FILENAMES[tf]}_{side}_{_DATE_RANGE[tf]}.csv"
    path = PROJECT_ROOT / fname
    if not path.exists():
        raise FileNotFoundError(path)
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]   # the header has "Volume "
    df = df.rename(columns={
        "Time (EET)": "time", "Open": "open", "High": "high",
        "Low": "low", "Close": "close", "Volume": "tick_volume",
    })
    df["time"] = pd.to_datetime(df["time"], format="%Y.%m.%d %H:%M:%S")
    # EET fixed as UTC+2 → UTC (see module docstring for the rationale).
    df["time"] = df["time"] - pd.Timedelta(hours=2)
    df = df.set_index("time").sort_index()
    return df


def _load_tf(tf: str, point: float) -> pd.DataFrame:
    """Load one TF as an MT5-compatible OHLC + spread DataFrame.

    The spread column is in MT5 "points" — the validator multiplies / divides
    by ``point`` to convert between price and points. We derive the spread per
    bar from ``(ask_close - bid_close) / point`` which is the bar-close spread
    the broker actually quoted at that moment.
    """
    bid = _load_csv(tf, "Bid")
    ask = _load_csv(tf, "Ask")
    common = bid.index.intersection(ask.index)
    bid = bid.loc[common]
    ask = ask.loc[common]
    spread_pts = ((ask["close"] - bid["close"]) / point).round().clip(lower=0)
    out = bid.copy()
    out["spread"] = spread_pts.astype("int64")
    # MT5's `real_volume` column is present in the live DataFrame; the
    # validator never reads it, but we include a zero column for shape parity.
    out["real_volume"] = 0
    return out


# --------------------------------------------------------------------------- #
# Fake connector — satisfies SignalValidator's contract
# --------------------------------------------------------------------------- #

class CsvConnector:
    """Connector stub that serves pre-loaded CSV DataFrames.

    ``SignalValidator`` only needs ``fetch_rates_parallel(bases, specs)``; the
    ``specs`` are honoured by *label only* — bar counts are ignored, the full
    CSV is returned. The validator's emit window is controlled separately via
    its ``history_bars`` constructor argument.
    """

    def __init__(self, frames_by_base: dict[str, dict[str, pd.DataFrame]]) -> None:
        self._frames = frames_by_base

    def fetch_rates_parallel(self, bases, specs):    # noqa: D401 — protocol method
        out: dict[tuple[str, str], pd.DataFrame] = {}
        for base in bases:
            per_tf = self._frames.get(base, {})
            for spec in specs:
                df = per_tf.get(spec.label)
                if df is not None and not df.empty:
                    out[(base, spec.label)] = df
        return out


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def _pct(x: float) -> str:
    return f"{x * 100:5.1f}%"


def _fmt_pf(pf: float) -> str:
    if pf == float("inf"):
        return "    ∞"
    return f"{pf:>6.2f}"


def _print_dataset_summary(frames: dict[str, pd.DataFrame]) -> None:
    print("=" * 78)
    print("DATA SUMMARY — XAUUSD (Dukascopy CSV)")
    print("=" * 78)
    for tf in ("W1", "D1", "H4", "H1", "M15"):
        df = frames[tf]
        spread = df["spread"]
        first = df.index.min().strftime("%Y-%m-%d %H:%M")
        last  = df.index.max().strftime("%Y-%m-%d %H:%M")
        print(f"  {tf:3s}  bars={len(df):>7,d}   "
              f"{first} → {last} UTC   "
              f"spread pts  mean={spread.mean():5.1f}  median={spread.median():5.0f}")
    print()


def _print_core(label: str, c) -> None:
    print(f"  ── {label} " + "─" * (72 - len(label)))
    print(f"    N trades            : {c.n_trades:>8,d}")
    print(f"    Win rate            : {_pct(c.win_rate)}     "
          f"(95% CI {_pct(c.ci_low)} – {_pct(c.ci_high)})")
    print(f"    Profit factor       : {_fmt_pf(c.profit_factor)}")
    print(f"    Expectancy (pts/tr) : {c.expectancy:>+8.1f}")
    print(f"    Max drawdown (pts)  : {c.max_drawdown:>8.1f}")
    print(f"    Avg MAE (pts)       : {c.avg_mae:>8.1f}")
    for i, t in enumerate(c.thirds, start=1):
        print(f"    Third #{i}            : "
              f"N={t.n_trades:>5,d}   win={_pct(t.win_rate)}   "
              f"exp={t.expectancy:>+8.1f}")
    print(f"    Trend  (ADX≥{config.BIAS_REGIME_ADX_HIGH:g}) : "
          f"N={c.regime_trend.n_trades:>5,d}   "
          f"win={_pct(c.regime_trend.win_rate)}   "
          f"exp={c.regime_trend.expectancy:>+8.1f}")
    print(f"    Range  (ADX< {config.BIAS_REGIME_ADX_HIGH:g}) : "
          f"N={c.regime_range.n_trades:>5,d}   "
          f"win={_pct(c.regime_range.win_rate)}   "
          f"exp={c.regime_range.expectancy:>+8.1f}")
    print(f"    Tier                : {c.tier}")
    print()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    # Windows terminals default to cp932 here, which cannot encode en/em
    # dashes or the tier labels (信頼 / 要注意). Force UTF-8 for stdout/stderr.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("backtest_xauusd")

    t_load0 = time.perf_counter()
    log.info("Loading CSVs (W1/D1/H4/H1/M15 × Bid+Ask)…")
    frames = {tf: _load_tf(tf, XAUUSD_POINT)
              for tf in ("W1", "D1", "H4", "H1", "M15")}
    log.info("Loaded in %.1fs", time.perf_counter() - t_load0)

    _print_dataset_summary(frames)

    connector = CsvConnector({"XAUUSD": frames})

    # The validator's emit window is the trailing ``history_bars`` of the base
    # TF. Set it past the longest TF (M15 ~382 k bars) so every base evaluates
    # its *entire* loaded history; smaller bases are capped by their own
    # length inside `_build_window` (``start = max(0, n - out_bars)``).
    emit_window = max(len(df) for df in frames.values()) + 100

    log.info("Running SignalValidator (history_bars=%d) — full 16y history…",
             emit_window)
    validator = SignalValidator(
        connector,
        history_bars=emit_window,
        fetch_gap_sec=0.0,                 # no inter-symbol pause needed
    )
    t0 = time.perf_counter()
    snap = validator.compute(
        bases=["XAUUSD"],
        broker_meta={"XAUUSD": {"point": XAUUSD_POINT}},
    )
    log.info("compute() finished in %.1fs", time.perf_counter() - t0)
    print()

    print("=" * 78)
    print("VALIDATION RESULTS — XAUUSD  (16y deep-history out-of-sample)")
    print("=" * 78)
    print("  Costs: per-bar Dukascopy bid/ask spread (more faithful than live)")
    print("  Tier thresholds: identical to dashboard live validator")
    print("  Macro/calendar filters: disabled  →  macro_filtered = raw")
    print()
    stats_by_tf = snap.by_symbol.get("XAUUSD", {})
    if not stats_by_tf:
        print("  No results produced. Check logs above.")
        return 1

    for base_tf in config.DWS_SMT_BASE_TFS:
        stats = stats_by_tf.get(base_tf)
        if stats is None:
            print(f"BASE = {base_tf} : no result")
            print()
            continue
        rows = " / ".join(config.DWS_SMT_STACKS[base_tf])
        print(f"BASE = {base_tf}   (3TF stack: {rows})")
        _print_core("raw", stats.raw)

    return 0


if __name__ == "__main__":
    sys.exit(main())
