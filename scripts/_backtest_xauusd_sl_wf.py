"""Walk-forward SL optimisation for XAUUSD over 16y Dukascopy history.

Builds on ``_backtest_xauusd.py``:

* Runs the production DWS-SMT trigger detector unchanged
  (``dws_smt.compute_symbol``) so the entry signals are identical to the live
  dashboard.
* **Replaces the trade pairer with an SL-aware version** that closes a trade
  the first time the bar's low (long) or high (short) crosses
  ``entry_price ∓ sl_mult × ATR(14)[entry]`` — otherwise the existing
  signal-out exit (opposite signal / EXIT) still fires.
* For each ``sl_mult ∈ {0.5, 1.0, 1.5, 2.0, 3.0, ∞}`` (``∞`` ≡ current
  signal-out baseline) and each base TF, splits the resulting trades by entry
  date — **train 2010–2018, test 2019–2025** — and reduces each split through
  the production :func:`signal_validator.evaluate_trades`. The reported
  ValidationCore is therefore directly comparable to the dashboard's tier.
* Picks the train-best SL and asks the data three questions: (a) does it stay
  ahead on the test period? (b) is the SL grid a plateau (robust) or a spike
  (over-fit)? (c) does it beat the signal-out baseline on test?

The script does NOT modify any production module. The SL pairer lives only
here; if a winning SL value warrants production roll-out that is a separate
landed change.
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from analyzer import dws_smt, indicators  # noqa: E402
from analyzer.dws_smt import DwsSmtTrade  # noqa: E402
from analyzer.signal_validator import evaluate_trades  # noqa: E402


# --------------------------------------------------------------------------- #
# CSV loading — same shape as _backtest_xauusd.py (duplicated for self-
# containment; this script is exploratory tooling, not a library).
# --------------------------------------------------------------------------- #

_TF_FILENAMES = {"W1": "Weekly", "D1": "Daily", "H4": "4 Hours",
                 "H1": "Hourly", "M15": "15 Mins"}
_DATE_RANGE = {
    "W1":  "2009.12.28_2025.12.29",
    "D1":  "2010.01.01_2025.12.31",
    "H4":  "2010.01.01_2025.12.31",
    "H1":  "2010.01.01_2025.12.31",
    "M15": "2010.01.01_2025.12.31",
}
# 3-digit pricing on Exness — matches analyzer/mt5_connector.py:325.
XAUUSD_POINT = 0.001


def _load_csv(tf: str, side: str) -> pd.DataFrame:
    fname = f"XAUUSD_{_TF_FILENAMES[tf]}_{side}_{_DATE_RANGE[tf]}.csv"
    df = pd.read_csv(PROJECT_ROOT / fname)
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={
        "Time (EET)": "time", "Open": "open", "High": "high",
        "Low": "low", "Close": "close", "Volume": "tick_volume",
    })
    df["time"] = pd.to_datetime(df["time"], format="%Y.%m.%d %H:%M:%S")
    df["time"] = df["time"] - pd.Timedelta(hours=2)   # EET fixed → UTC
    return df.set_index("time").sort_index()


def _load_tf(tf: str, point: float) -> pd.DataFrame:
    bid = _load_csv(tf, "Bid")
    ask = _load_csv(tf, "Ask")
    common = bid.index.intersection(ask.index)
    bid, ask = bid.loc[common], ask.loc[common]
    spread_pts = ((ask["close"] - bid["close"]) / point).round().clip(lower=0)
    out = bid.copy()
    out["spread"] = spread_pts.astype("int64")
    return out


# --------------------------------------------------------------------------- #
# SL-aware trade pairer
# --------------------------------------------------------------------------- #

def _trade_mae_arr(direction: int, entry_price: float, highs: np.ndarray,
                   lows: np.ndarray, entry_idx: int, exit_idx: int) -> float:
    """Same convention as ``dws_smt._trade_mae``."""
    if exit_idx < entry_idx:
        return 0.0
    if direction == 1:
        worst = float(lows[entry_idx:exit_idx + 1].min())
        return max(0.0, entry_price - worst)
    worst = float(highs[entry_idx:exit_idx + 1].max())
    return max(0.0, worst - entry_price)


def pair_trades_with_sl(
    triggers: tuple,
    closes: np.ndarray, highs: np.ndarray, lows: np.ndarray,
    atr: np.ndarray, sl_mult: float,
) -> tuple[DwsSmtTrade, ...]:
    """SL-aware port of ``dws_smt._pair_trades``.

    Iterates every bar (not just trigger bars). For each bar after a trade's
    entry: if the bar's adverse extreme crosses ``entry_price ∓ sl_mult ×
    atr[entry_idx]`` the trade closes at the SL price. Otherwise the trade
    still closes on the next opposite signal / EXIT (signal-out fallback).

    ``sl_mult == math.inf`` disables SL → identical to ``_pair_trades``
    (verified in the baseline row of the report).
    """
    trades: list[DwsSmtTrade] = []
    pos_dir = 0
    pos_entry_idx = -1
    pos_entry_price = 0.0
    n = len(triggers)
    use_sl = np.isfinite(sl_mult) and sl_mult > 0.0

    def _close(exit_idx: int, exit_price: float, is_open: bool) -> None:
        trades.append(DwsSmtTrade(
            entry_idx=pos_entry_idx, direction=pos_dir,
            points=(exit_price - pos_entry_price) * pos_dir,
            mae=_trade_mae_arr(pos_dir, pos_entry_price, highs, lows,
                               pos_entry_idx, exit_idx),
            is_open=is_open,
        ))

    for j in range(n):
        # ---- 1. Stop-loss check (intra-bar, fires before close-of-bar trigger).
        if use_sl and pos_dir != 0 and j > pos_entry_idx:
            entry_atr = float(atr[pos_entry_idx]) if pos_entry_idx < atr.size else 0.0
            if np.isfinite(entry_atr) and entry_atr > 0.0:
                sl_dist = sl_mult * entry_atr
                if pos_dir == 1:
                    sl_price = pos_entry_price - sl_dist
                    if lows[j] <= sl_price:
                        _close(j, sl_price, is_open=False)
                        pos_dir = 0
                else:
                    sl_price = pos_entry_price + sl_dist
                    if highs[j] >= sl_price:
                        _close(j, sl_price, is_open=False)
                        pos_dir = 0

        # ---- 2. Signal at this bar's close (identical to _pair_trades).
        g = triggers[j]
        if g is None:
            continue
        price = float(closes[j])
        if g in ("BUY", "SELL"):
            new_dir = 1 if g == "BUY" else -1
            if pos_dir not in (0, new_dir):       # reversal → close first
                _close(j, price, is_open=False)
            if pos_dir != new_dir:
                pos_dir, pos_entry_idx, pos_entry_price = new_dir, j, price
        elif g == "EXIT" and pos_dir != 0:
            _close(j, price, is_open=False)
            pos_dir = 0

    if pos_dir != 0:
        _close(n - 1, float(closes[n - 1]), is_open=True)
    return tuple(trades)


# --------------------------------------------------------------------------- #
# Walk-forward evaluation
# --------------------------------------------------------------------------- #

# 2019-01-01 UTC — separates train (2010-2018, ~9 y) from test (2019-2025, ~7 y).
SPLIT_DATE = pd.Timestamp("2019-01-01", tz="UTC")
SPLIT_MS = int(SPLIT_DATE.timestamp() * 1000)

SL_GRID = (0.5, 1.0, 1.5, 2.0, 3.0, float("inf"))


@dataclass(frozen=True)
class WfRow:
    """One (base_tf, sl_mult) walk-forward evaluation result."""
    sl_mult: float
    train: object       # ValidationCore
    test: object        # ValidationCore


def evaluate_one_sl(
    window, base_df: pd.DataFrame, point: float, sl_mult: float,
) -> WfRow:
    """Re-pair trades with ``sl_mult`` and evaluate train/test splits."""
    # Window arrays
    closes = base_df["close"].to_numpy(dtype=np.float64)
    highs = base_df["high"].to_numpy(dtype=np.float64)
    lows = base_df["low"].to_numpy(dtype=np.float64)
    n_bars = len(base_df)
    emitted = window.times_ms.size
    start = max(0, n_bars - emitted)

    # ATR(14) on the full base history; slice to the emitted window so the
    # trade entry_idx (which is window-relative) lines up.
    atr_full = indicators.atr(highs[None, :], lows[None, :], closes[None, :],
                              config.ATR_PERIOD)[0]
    atr = np.nan_to_num(atr_full[start:], nan=0.0)

    # The window arrays the pairer needs (sliced to emit window).
    closes_w = closes[start:]
    highs_w = highs[start:]
    lows_w = lows[start:]

    trades = pair_trades_with_sl(window.triggers, closes_w, highs_w, lows_w,
                                 atr, sl_mult)

    # Spread + ADX inputs for evaluate_trades, both window-aligned.
    if "spread" in base_df.columns:
        spread_pts = base_df["spread"].to_numpy(dtype=np.float64)[start:]
    else:
        spread_pts = np.zeros(emitted, dtype=np.float64)
    adx_2d, _, _ = indicators.adx(highs[None, :], lows[None, :], closes[None, :],
                                  config.ADX_PERIOD)
    adx = np.nan_to_num(adx_2d[0][start:], nan=0.0)

    # Date-split closed trades by entry bar time.
    train_trades, test_trades = [], []
    for t in trades:
        if t.is_open or t.entry_idx >= window.times_ms.size:
            continue
        bar_ms = int(window.times_ms[t.entry_idx])
        (train_trades if bar_ms < SPLIT_MS else test_trades).append(t)

    train_core = evaluate_trades(tuple(train_trades), spread_pts=spread_pts,
                                 adx=adx, point=point)
    test_core = evaluate_trades(tuple(test_trades), spread_pts=spread_pts,
                                adx=adx, point=point)
    return WfRow(sl_mult=sl_mult, train=train_core, test=test_core)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def _fmt_sl(m: float) -> str:
    return "∞ (signal-out)" if not np.isfinite(m) else f"{m:>3.1f}×ATR     "


def _pct(x: float) -> str:
    return f"{x * 100:5.1f}%"


def _fmt_pf(pf: float) -> str:
    return "   ∞" if pf == float("inf") else f"{pf:>5.2f}"


def _row(label: str, c) -> str:
    return (f"  N={c.n_trades:>5,d}  win={_pct(c.win_rate)}  "
            f"PF={_fmt_pf(c.profit_factor)}  exp={c.expectancy:>+8.1f}  "
            f"DD={c.max_drawdown:>7.0f}  tier={c.tier}")


def print_wf_table(base_tf: str, rows: list[WfRow]) -> None:
    stack = " / ".join(config.DWS_SMT_STACKS[base_tf])
    print(f"BASE = {base_tf}   (3TF stack: {stack})")
    print("-" * 78)
    for r in rows:
        print(f"  SL = {_fmt_sl(r.sl_mult)}")
        print(f"    TRAIN (2010-2018):" + _row("", r.train))
        print(f"    TEST  (2019-2025):" + _row("", r.test))
    print()


def print_summary(base_tf: str, rows: list[WfRow]) -> None:
    """Compare train-best SL against the signal-out baseline on TEST data."""
    baseline = next(r for r in rows if not np.isfinite(r.sl_mult))
    finite = [r for r in rows if np.isfinite(r.sl_mult)]
    train_best = max(finite, key=lambda r: r.train.profit_factor)
    test_best  = max(finite, key=lambda r: r.test.profit_factor)

    print(f"  [SUMMARY {base_tf}]")
    print(f"    Baseline (∞ signal-out)        TEST  PF={_fmt_pf(baseline.test.profit_factor)}  "
          f"exp={baseline.test.expectancy:>+8.1f}  DD={baseline.test.max_drawdown:>7.0f}")
    print(f"    Best SL by TRAIN  ={train_best.sl_mult:>3.1f}×ATR  →  "
          f"TEST  PF={_fmt_pf(train_best.test.profit_factor)}  "
          f"exp={train_best.test.expectancy:>+8.1f}  DD={train_best.test.max_drawdown:>7.0f}")
    print(f"    Best SL by TEST   ={test_best.sl_mult:>3.1f}×ATR  →  "
          f"TEST  PF={_fmt_pf(test_best.test.profit_factor)}  "
          f"exp={test_best.test.expectancy:>+8.1f}  DD={test_best.test.max_drawdown:>7.0f}")

    # Plateau check: any other SL within 10% of train-best?
    near = [r.sl_mult for r in finite
            if abs(r.train.profit_factor - train_best.train.profit_factor)
               / max(train_best.train.profit_factor, 1e-9) <= 0.10
            and r.sl_mult != train_best.sl_mult]
    if near:
        print(f"    Plateau: {len(near)+1} SL values within 10% of best train PF "
              f"({sorted(near + [train_best.sl_mult])}) → robust region")
    else:
        print("    Plateau: train-best is isolated → spike (overfit risk)")

    # Robustness verdict.
    train_pf = train_best.train.profit_factor
    test_pf  = train_best.test.profit_factor
    deg = (train_pf - test_pf) / max(train_pf, 1e-9)
    if test_pf >= baseline.test.profit_factor and deg < 0.20:
        verdict = "ROBUST ✓  (out-of-sample beat baseline, <20% degradation)"
    elif test_pf >= baseline.test.profit_factor:
        verdict = "MARGINAL  (beat baseline but train→test degraded >20%)"
    else:
        verdict = "OVERFIT ✗  (lost to baseline out-of-sample)"
    print(f"    Verdict: {verdict}")
    print()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("backtest_sl_wf")

    log.info("Loading CSVs (W1/D1/H4/H1/M15)…")
    t0 = time.perf_counter()
    frames = {tf: _load_tf(tf, XAUUSD_POINT)
              for tf in ("W1", "D1", "H4", "H1", "M15")}
    log.info("Loaded in %.1fs", time.perf_counter() - t0)

    log.info("Running DWS-SMT trigger detection (full history)…")
    t0 = time.perf_counter()
    emit_window = max(len(df) for df in frames.values()) + 100
    result = dws_smt.compute_symbol(frames, out_bars=emit_window)
    if result is None:
        log.error("dws_smt.compute_symbol returned None")
        return 1
    log.info("compute_symbol in %.1fs", time.perf_counter() - t0)

    print()
    print("=" * 78)
    print("WALK-FORWARD SL OPTIMISATION — XAUUSD (16y Dukascopy)")
    print("=" * 78)
    print(f"  Train: 2010-01-01 → {SPLIT_DATE.strftime('%Y-%m-%d')} (~9 y)")
    print(f"  Test : {SPLIT_DATE.strftime('%Y-%m-%d')} → 2025-12-31 (~7 y)")
    print(f"  SL grid: {[ '∞' if not np.isfinite(s) else f'{s:.1f}×ATR' for s in SL_GRID ]}")
    print(f"  Costs: per-bar Dukascopy bid/ask spread (faithful)")
    print()

    for base_tf in config.DWS_SMT_BASE_TFS:
        window = result.by_base.get(base_tf)
        base_df = frames.get(base_tf)
        if window is None or base_df is None:
            print(f"BASE = {base_tf}: no data\n")
            continue

        t0 = time.perf_counter()
        rows = [evaluate_one_sl(window, base_df, XAUUSD_POINT, m) for m in SL_GRID]
        log.info("%s: 6 SL evals in %.1fs", base_tf, time.perf_counter() - t0)

        print_wf_table(base_tf, rows)
        print_summary(base_tf, rows)
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
