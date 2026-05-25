"""Yearly stability backtest — buckets every trade by entry calendar year and
reports per-year PF / win rate / tier per (symbol, base TF) for the signal-out
strategy with swap costs included.

This is precision-review item ② (rolling walk-forward). The earlier scripts
established that signal-out beats every fixed-SL grid on average over a single
2010-2018 / 2019-2025 split. This script answers the next question:

    "Does the edge hold year-over-year, or does the average hide a bad year
     we should know about?"

Because the DWS-SMT strategy is parameter-free, true train/test windows
aren't needed — each calendar year is its own independent OOS observation.
We report 2011-2025 (skipping 2010 to let W1 EMA(20) etc. fully warm up
from the 2009-12-28 Dukascopy weekly start).

Output
------
Per cell: year-by-year table with N, win rate, PF, expectancy, DD, tier.
Consolidated: "Years tier 信頼", "Worst PF year", "Best PF year", "Avg PF"
across all 10 × 3 = 30 cells. Cells where ANY year fails to reach tier 信頼
or where PF drops below 1.0 are highlighted so you can decide whether the
weakness is regime-specific (acceptable) or strategy-rotting (not).

Swap costs from data/historical_rates.csv are applied per trade as in
``_backtest_all_sl_wf_swap.py``.
"""

from __future__ import annotations

import gc
import logging
import statistics
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
# Symbols / TF layout — same as the SL-WF script
# --------------------------------------------------------------------------- #

POINT_BY_SYMBOL: dict[str, float] = {
    "XAUUSD": 0.001,
    "USDJPY": 0.001, "EURJPY": 0.001, "GBPJPY": 0.001, "AUDJPY": 0.001,
    "EURUSD": 0.00001, "GBPUSD": 0.00001, "AUDUSD": 0.00001,
    "EURGBP": 0.00001, "EURAUD": 0.00001,
}

_TF_FILENAMES = {"W1": "Weekly", "D1": "Daily", "H4": "4 Hours",
                 "H1": "Hourly", "M15": "15 Mins"}
_DATE_RANGE = {
    "W1":  "2009.12.28_2025.12.29",
    "D1":  "2010.01.01_2025.12.31",
    "H4":  "2010.01.01_2025.12.31",
    "H1":  "2010.01.01_2025.12.31",
    "M15": "2010.01.01_2025.12.31",
}

RATES_FILE = PROJECT_ROOT / "data" / "historical_rates.csv"

YEAR_FIRST = 2011    # skip 2010 — W1 EMA(20) still warming up
YEAR_LAST  = 2025


# --------------------------------------------------------------------------- #
# CSV loading + rates (same convention as the SL-WF + swap script)
# --------------------------------------------------------------------------- #

def _load_csv(symbol, tf, side):
    fname = f"{symbol}_{_TF_FILENAMES[tf]}_{side}_{_DATE_RANGE[tf]}.csv"
    df = pd.read_csv(PROJECT_ROOT / fname)
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={
        "Time (EET)": "time", "Open": "open", "High": "high",
        "Low": "low", "Close": "close", "Volume": "tick_volume",
    })
    df["time"] = pd.to_datetime(df["time"], format="%Y.%m.%d %H:%M:%S")
    df["time"] = df["time"] - pd.Timedelta(hours=2)
    return df.set_index("time").sort_index()


def _load_tf(symbol, tf, point):
    bid = _load_csv(symbol, tf, "Bid")
    ask = _load_csv(symbol, tf, "Ask")
    common = bid.index.intersection(ask.index)
    bid, ask = bid.loc[common], ask.loc[common]
    spread_pts = ((ask["close"] - bid["close"]) / point).round().clip(lower=0)
    out = bid.copy()
    out["spread"] = spread_pts.astype("int64")
    return out


def load_symbol(symbol):
    point = POINT_BY_SYMBOL[symbol]
    return {tf: _load_tf(symbol, tf, point)
            for tf in ("W1", "D1", "H4", "H1", "M15")}


def load_rates() -> pd.DataFrame:
    if not RATES_FILE.exists():
        raise FileNotFoundError(
            f"{RATES_FILE} missing — run scripts/_fetch_historical_rates.py first")
    return pd.read_csv(RATES_FILE, parse_dates=["date"]).set_index("date").sort_index()


def rate_at(rates, ccy, dt_ns):
    if ccy == "XAU" or ccy not in rates.columns:
        return 0.0
    ts = pd.Timestamp(dt_ns, unit="ns").normalize()
    if ts < rates.index[0]:
        return float(rates[ccy].iloc[0])
    if ts > rates.index[-1]:
        return float(rates[ccy].iloc[-1])
    return float(rates.loc[ts, ccy])


# --------------------------------------------------------------------------- #
# Signal-out trade pairer + swap application (extracted from _sl_wf_swap)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class BtTrade:
    entry_idx: int
    exit_idx: int
    direction: int
    points: float
    mae: float
    is_open: bool


def _trade_mae(direction, entry_price, highs, lows, entry_idx, exit_idx):
    if exit_idx < entry_idx:
        return 0.0
    if direction == 1:
        worst = float(lows[entry_idx:exit_idx + 1].min())
        return max(0.0, entry_price - worst)
    worst = float(highs[entry_idx:exit_idx + 1].max())
    return max(0.0, worst - entry_price)


def pair_trades_signal_out(triggers, closes, highs, lows):
    """Signal-out only — SL disabled. Identical to ``dws_smt._pair_trades``
    but records ``exit_idx`` per trade so we can compute swap holding cost."""
    trades: list[BtTrade] = []
    pos_dir = 0
    pos_entry_idx = -1
    pos_entry_price = 0.0
    n = len(triggers)

    def _close(exit_idx, exit_price, is_open):
        trades.append(BtTrade(
            entry_idx=pos_entry_idx, exit_idx=exit_idx,
            direction=pos_dir,
            points=(exit_price - pos_entry_price) * pos_dir,
            mae=_trade_mae(pos_dir, pos_entry_price, highs, lows,
                           pos_entry_idx, exit_idx),
            is_open=is_open,
        ))

    for j in range(n):
        g = triggers[j]
        if g is None:
            continue
        price = float(closes[j])
        if g in ("BUY", "SELL"):
            new_dir = 1 if g == "BUY" else -1
            if pos_dir not in (0, new_dir):
                _close(j, price, is_open=False)
            if pos_dir != new_dir:
                pos_dir, pos_entry_idx, pos_entry_price = new_dir, j, price
        elif g == "EXIT" and pos_dir != 0:
            _close(j, price, is_open=False)
            pos_dir = 0
    if pos_dir != 0:
        _close(n - 1, float(closes[n - 1]), is_open=True)
    return tuple(trades)


NS_PER_DAY = 86_400_000_000_000


def apply_swap_costs(bt_trades, symbol, times_ns, closes, rates):
    base_ccy = symbol[:3]
    quote_ccy = symbol[3:]
    out: list[DwsSmtTrade] = []
    for t in bt_trades:
        if t.is_open:
            out.append(DwsSmtTrade(
                entry_idx=t.entry_idx, direction=t.direction,
                points=t.points, mae=t.mae, is_open=True,
            ))
            continue
        if t.entry_idx >= times_ns.size or t.exit_idx >= times_ns.size:
            out.append(DwsSmtTrade(
                entry_idx=t.entry_idx, direction=t.direction,
                points=t.points, mae=t.mae, is_open=False,
            ))
            continue
        days_held = max(0.0, (times_ns[t.exit_idx] - times_ns[t.entry_idx]) / NS_PER_DAY)
        base_rate = rate_at(rates, base_ccy, int(times_ns[t.entry_idx]))
        quote_rate = rate_at(rates, quote_ccy, int(times_ns[t.entry_idx]))
        entry_price = float(closes[t.entry_idx])
        swap = entry_price * (base_rate - quote_rate) * t.direction / 365.0 / 100.0 * days_held
        out.append(DwsSmtTrade(
            entry_idx=t.entry_idx, direction=t.direction,
            points=t.points + swap, mae=t.mae, is_open=False,
        ))
    return tuple(out)


# --------------------------------------------------------------------------- #
# Yearly bucketing
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class YearResult:
    year: int
    core: object        # ValidationCore


@dataclass(frozen=True)
class CellResult:
    symbol: str
    base_tf: str
    years: list[YearResult]
    overall: object     # ValidationCore across all years


def evaluate_cell(window, base_df, symbol, point, rates) -> CellResult:
    closes = base_df["close"].to_numpy(dtype=np.float64)
    highs = base_df["high"].to_numpy(dtype=np.float64)
    lows = base_df["low"].to_numpy(dtype=np.float64)
    n_bars = len(base_df)
    emitted = window.times_ms.size
    start = max(0, n_bars - emitted)

    closes_w = closes[start:]
    highs_w = highs[start:]
    lows_w = lows[start:]

    bt_trades = pair_trades_signal_out(window.triggers, closes_w, highs_w, lows_w)
    times_ns = window.times_ms.astype(np.int64) * 1_000_000
    trades = apply_swap_costs(bt_trades, symbol, times_ns, closes_w, rates)

    spread_pts = (base_df["spread"].to_numpy(dtype=np.float64)[start:]
                  if "spread" in base_df.columns
                  else np.zeros(emitted, dtype=np.float64))
    adx_2d, _, _ = indicators.adx(highs[None, :], lows[None, :], closes[None, :],
                                  config.ADX_PERIOD)
    adx = np.nan_to_num(adx_2d[0][start:], nan=0.0)

    # Bucket trades by entry year. Skip open trades and trades before YEAR_FIRST.
    entry_years = np.where(
        np.arange(window.times_ms.size) < window.times_ms.size,
        pd.to_datetime(window.times_ms, unit="ms").year, 0)
    by_year: dict[int, list] = {y: [] for y in range(YEAR_FIRST, YEAR_LAST + 1)}
    all_trades: list = []
    for t in trades:
        if t.is_open or t.entry_idx >= window.times_ms.size:
            continue
        yr = int(entry_years[t.entry_idx])
        if yr < YEAR_FIRST or yr > YEAR_LAST:
            continue
        by_year[yr].append(t)
        all_trades.append(t)

    years = []
    for y in range(YEAR_FIRST, YEAR_LAST + 1):
        core = evaluate_trades(tuple(by_year[y]), spread_pts=spread_pts,
                               adx=adx, point=point)
        years.append(YearResult(year=y, core=core))

    overall = evaluate_trades(tuple(all_trades), spread_pts=spread_pts,
                              adx=adx, point=point)
    return CellResult(symbol=symbol, base_tf=window.base_tf,
                      years=years, overall=overall)


def run_symbol(symbol: str, rates: pd.DataFrame) -> list[CellResult]:
    log = logging.getLogger("backtest_yearly")
    t0 = time.perf_counter()
    frames = load_symbol(symbol)
    point = POINT_BY_SYMBOL[symbol]
    emit_window = max(len(df) for df in frames.values()) + 100

    result = dws_smt.compute_symbol(frames, out_bars=emit_window)
    if result is None:
        log.error("%s: dws_smt.compute_symbol returned None", symbol)
        return []

    out = []
    for base_tf in config.DWS_SMT_BASE_TFS:
        window = result.by_base.get(base_tf)
        base_df = frames.get(base_tf)
        if window is None or base_df is None:
            continue
        out.append(evaluate_cell(window, base_df, symbol, point, rates))
    log.info("%-7s done in %.1fs", symbol, time.perf_counter() - t0)
    del frames, result
    gc.collect()
    return out


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def _pct(x):
    return f"{x * 100:5.1f}%"


def _fmt_pf(pf):
    return "   ∞" if pf == float("inf") else f"{pf:>5.2f}"


def print_cell_detail(cell: CellResult) -> None:
    stack = " / ".join(config.DWS_SMT_STACKS[cell.base_tf])
    print(f"\n{'=' * 78}")
    print(f"  {cell.symbol}  {cell.base_tf}  (3TF stack: {stack})")
    print('=' * 78)
    print(f"  {'Year':<5}  {'N':>5}  {'Win':>6}  {'PF':>6}  "
          f"{'Exp':>10}  {'DD':>9}  tier")
    for yr in cell.years:
        c = yr.core
        print(f"  {yr.year:<5}  {c.n_trades:>5,d}  {_pct(c.win_rate):>6}  "
              f"{_fmt_pf(c.profit_factor):>6}  {c.expectancy:>+10.1f}  "
              f"{c.max_drawdown:>9.0f}  {c.tier}")
    # Summary across years
    pfs = [y.core.profit_factor for y in cell.years
           if y.core.n_trades > 0 and y.core.profit_factor != float("inf")]
    tiers = [y.core.tier for y in cell.years if y.core.n_trades > 0]
    trusted = sum(1 for t in tiers if t == "信頼")
    insufficient = sum(1 for t in tiers if t == "データ不足")
    caution = sum(1 for t in tiers if t == "要注意")
    print()
    print(f"  OVERALL (all years):  N={cell.overall.n_trades:,d}  "
          f"PF={_fmt_pf(cell.overall.profit_factor)}  "
          f"win={_pct(cell.overall.win_rate)}  tier={cell.overall.tier}")
    if pfs:
        print(f"  Per-year PFs:  min={min(pfs):.2f}  max={max(pfs):.2f}  "
              f"mean={statistics.mean(pfs):.2f}  "
              f"stdev={statistics.stdev(pfs) if len(pfs) > 1 else 0:.2f}")
    print(f"  Years 信頼={trusted}  要注意={caution}  データ不足={insufficient}  "
          f"(of {len(cell.years)})")
    # Flag bad years (PF < 1)
    bad = [(y.year, y.core.profit_factor) for y in cell.years
           if y.core.n_trades > 0 and y.core.profit_factor < 1.0]
    if bad:
        print(f"  ⚠ Losing years (PF < 1):  {bad}")


def print_consolidated(cells: list[CellResult]) -> None:
    print()
    print("=" * 100)
    print(" CONSOLIDATED — yearly stability (signal-out + swap costs)")
    print("=" * 100)
    hdr = f"  {'Symbol':<8}  {'Base':<4}  "
    hdr += f"{'AllN':>7}  {'AllPF':>6}  "
    hdr += f"{'信頼/N':>7}  {'MinPF':>6}  {'MaxPF':>6}  {'AvgPF':>6}  {'σ':>5}  "
    hdr += "BadYrs"
    print(hdr)
    print(f"  {'-' * 8}  {'-' * 4}  {'-' * 7}  {'-' * 6}  {'-' * 7}  "
          f"{'-' * 6}  {'-' * 6}  {'-' * 6}  {'-' * 5}  {'-' * 6}")
    for c in cells:
        pfs = [y.core.profit_factor for y in c.years
               if y.core.n_trades > 0 and y.core.profit_factor != float("inf")]
        tiers = [y.core.tier for y in c.years if y.core.n_trades > 0]
        n_eval = len(tiers)
        trusted = sum(1 for t in tiers if t == "信頼")
        bad = [str(y.year) for y in c.years
               if y.core.n_trades > 0 and y.core.profit_factor < 1.0]
        sigma = statistics.stdev(pfs) if len(pfs) > 1 else 0.0
        print(f"  {c.symbol:<8}  {c.base_tf:<4}  "
              f"{c.overall.n_trades:>7,d}  "
              f"{_fmt_pf(c.overall.profit_factor):>6}  "
              f"{trusted:>3}/{n_eval:<3}  "
              f"{min(pfs) if pfs else 0:>6.2f}  "
              f"{max(pfs) if pfs else 0:>6.2f}  "
              f"{statistics.mean(pfs) if pfs else 0:>6.2f}  "
              f"{sigma:>5.2f}  "
              f"{','.join(bad) if bad else '-'}")
    # Final aggregate
    all_pfs = []
    all_trusted = 0
    all_eval = 0
    cells_with_bad = 0
    for c in cells:
        pfs = [y.core.profit_factor for y in c.years
               if y.core.n_trades > 0 and y.core.profit_factor != float("inf")]
        tiers = [y.core.tier for y in c.years if y.core.n_trades > 0]
        all_pfs += pfs
        all_trusted += sum(1 for t in tiers if t == "信頼")
        all_eval += len(tiers)
        if any(y.core.n_trades > 0 and y.core.profit_factor < 1.0 for y in c.years):
            cells_with_bad += 1
    print()
    print("  Across all (symbol × base TF × year) buckets:")
    print(f"    Total year-cells evaluated: {all_eval}")
    print(f"    Year-cells reaching 信頼:    {all_trusted}/{all_eval} "
          f"({all_trusted*100/max(1,all_eval):.0f}%)")
    print(f"    Mean PF: {statistics.mean(all_pfs) if all_pfs else 0:.2f}  "
          f"Median PF: {statistics.median(all_pfs) if all_pfs else 0:.2f}")
    print(f"    Cells with any losing year (PF<1): {cells_with_bad} / {len(cells)}")


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
    log = logging.getLogger("backtest_yearly")

    rates = load_rates()
    log.info("Loaded historical rates: %d daily rows", len(rates))

    symbols = [s.base for s in config.SYMBOLS]
    log.info("Running yearly stability backtest on %d symbols (years %d-%d)",
             len(symbols), YEAR_FIRST, YEAR_LAST)

    t0 = time.perf_counter()
    all_cells: list[CellResult] = []
    for sym in symbols:
        if sym not in POINT_BY_SYMBOL:
            continue
        try:
            cells = run_symbol(sym, rates)
        except FileNotFoundError as e:
            log.warning("%s: CSV missing — skipping (%s)", sym, e)
            continue
        all_cells.extend(cells)
    log.info("All symbols done in %.1fs", time.perf_counter() - t0)

    for cell in all_cells:
        print_cell_detail(cell)
    print_consolidated(all_cells)
    return 0


if __name__ == "__main__":
    sys.exit(main())
