"""All-symbols walk-forward SL backtest WITH overnight-swap cost modelling.

Extension of ``_backtest_all_sl_wf.py``. The earlier backtest ignored the
broker swap (rollover) charge — for trades that span multiple days this
materially understates the cost on negative-carry directions (notably JPY
crosses shorting the higher-yielding currency, e.g. SHORT USDJPY during a
high-rate-differential era).

Model
-----
For each closed trade we compute

    swap_per_day = entry_price × (base_rate − quote_rate) × direction / 365 / 100
    total_swap   = swap_per_day × days_held

and subtract it from the trade's price-unit gain (so a *negative* carry
trade has its ``net`` reduced before :func:`signal_validator.evaluate_trades`
ever sees it).

Rates come from ``data/historical_rates.csv`` (monthly OECD-harmonised
policy rate proxies for USD/EUR/GBP/JPY/AUD, forward-filled to daily —
generated once by ``scripts/_fetch_historical_rates.py``). XAU has no
interest rate of its own; we treat it as 0 (so the swap of an XAUUSD
position is driven purely by the USD-side leg — long pays USD rate, short
receives it — which is the textbook commodity-futures convention).

Caveats disclosed
-----------------
* Monthly granularity on rate observations — fine since policy rates are
  step functions that change at most ~10×/year per central bank.
* No broker bid/ask markup on the swap (typically +0.3 to +1% added to
  the rate differential). Real-world swap costs are therefore *somewhat
  worse* than what this models — every conclusion that says "signal-out
  still wins after swap" is biased on the conservative side.
* No Wednesday triple-swap (3× charge for Wed→Thu rollover).
* No storage cost on XAU above the USD-leg model (~0.1-0.5% per year for
  physical, often built into the broker quote).
"""

from __future__ import annotations

import gc
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
# Per-symbol layout (same as the no-swap script)
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


# --------------------------------------------------------------------------- #
# CSV loading
# --------------------------------------------------------------------------- #

def _load_csv(symbol: str, tf: str, side: str) -> pd.DataFrame:
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


def _load_tf(symbol: str, tf: str, point: float) -> pd.DataFrame:
    bid = _load_csv(symbol, tf, "Bid")
    ask = _load_csv(symbol, tf, "Ask")
    common = bid.index.intersection(ask.index)
    bid, ask = bid.loc[common], ask.loc[common]
    spread_pts = ((ask["close"] - bid["close"]) / point).round().clip(lower=0)
    out = bid.copy()
    out["spread"] = spread_pts.astype("int64")
    return out


def load_symbol(symbol: str) -> dict[str, pd.DataFrame]:
    point = POINT_BY_SYMBOL[symbol]
    return {tf: _load_tf(symbol, tf, point)
            for tf in ("W1", "D1", "H4", "H1", "M15")}


# --------------------------------------------------------------------------- #
# Historical rates lookup
# --------------------------------------------------------------------------- #

def load_rates() -> pd.DataFrame:
    """Daily DataFrame indexed by date, columns = USD/EUR/GBP/JPY/AUD percent."""
    if not RATES_FILE.exists():
        raise FileNotFoundError(
            f"{RATES_FILE} missing — run scripts/_fetch_historical_rates.py first")
    df = pd.read_csv(RATES_FILE, parse_dates=["date"]).set_index("date").sort_index()
    return df


def rate_at(rates: pd.DataFrame, ccy: str, dt_ns: int) -> float:
    """Look up the policy rate (% annual) for *ccy* at epoch-ns ``dt_ns``.

    XAU has no rate of its own — we return 0 so the swap formula reduces
    to "long pays / short receives the USD-side rate" for XAUUSD.
    """
    if ccy == "XAU":
        return 0.0
    if ccy not in rates.columns:
        return 0.0
    ts = pd.Timestamp(dt_ns, unit="ns")
    # The rates DataFrame is already forward-filled at daily resolution; just
    # snap to the trade-entry date.
    date = ts.normalize()
    if date < rates.index[0]:
        return float(rates[ccy].iloc[0])
    if date > rates.index[-1]:
        return float(rates[ccy].iloc[-1])
    return float(rates.loc[date, ccy])


# --------------------------------------------------------------------------- #
# SL-aware trade pairer — same as before, but ALSO records exit_idx per trade
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class BtTrade:
    """Backtest trade with explicit entry/exit indices so we can compute
    swap costs from the holding period. ``points`` and ``mae`` use the
    same conventions as :class:`dws_smt.DwsSmtTrade`."""
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


def pair_trades_with_sl(triggers, closes, highs, lows, atr, sl_mult):
    trades: list[BtTrade] = []
    pos_dir = 0
    pos_entry_idx = -1
    pos_entry_price = 0.0
    n = len(triggers)
    use_sl = np.isfinite(sl_mult) and sl_mult > 0.0

    def _close(exit_idx, exit_price, is_open):
        trades.append(BtTrade(
            entry_idx=pos_entry_idx,
            exit_idx=exit_idx,
            direction=pos_dir,
            points=(exit_price - pos_entry_price) * pos_dir,
            mae=_trade_mae(pos_dir, pos_entry_price, highs, lows,
                           pos_entry_idx, exit_idx),
            is_open=is_open,
        ))

    for j in range(n):
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


# --------------------------------------------------------------------------- #
# Swap-cost application
# --------------------------------------------------------------------------- #

NS_PER_DAY = 86_400_000_000_000


def apply_swap_costs(
    bt_trades: tuple[BtTrade, ...],
    symbol: str,
    times_ns: np.ndarray,
    closes: np.ndarray,
    rates: pd.DataFrame,
) -> tuple[DwsSmtTrade, ...]:
    """Adjust each trade's ``points`` for overnight swap, then return the
    set of equivalent :class:`DwsSmtTrade` objects suitable for
    :func:`evaluate_trades`.

    Currency derivation: ``base = symbol[:3]`` (XAUUSD → XAU), ``quote =
    symbol[3:]`` (XAUUSD → USD). XAU has no rate (treated as 0).
    """
    base_ccy = symbol[:3]
    quote_ccy = symbol[3:]
    out: list[DwsSmtTrade] = []
    for t in bt_trades:
        if t.is_open:
            # is_open trades are filtered by evaluate_trades anyway, but keep
            # them passing through so the shape stays consistent.
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
        # Entry price reconstruction from the close at entry — the SL pairer
        # used closes[entry_idx] as the entry, so the same value here.
        entry_price = float(closes[t.entry_idx])
        swap_per_day = (entry_price * (base_rate - quote_rate)
                        * t.direction / 365.0 / 100.0)
        total_swap = swap_per_day * days_held
        adjusted_points = t.points + total_swap  # swap is signed
        out.append(DwsSmtTrade(
            entry_idx=t.entry_idx,
            direction=t.direction,
            points=adjusted_points,
            mae=t.mae,
            is_open=False,
        ))
    return tuple(out)


# --------------------------------------------------------------------------- #
# Walk-forward evaluation per (symbol, base TF, sl_mult)
# --------------------------------------------------------------------------- #

SPLIT_DATE = pd.Timestamp("2019-01-01", tz="UTC")
SPLIT_MS = int(SPLIT_DATE.timestamp() * 1000)
SL_GRID = (0.5, 1.0, 1.5, 2.0, 3.0, float("inf"))


@dataclass(frozen=True)
class WfRow:
    sl_mult: float
    train: object
    test: object


@dataclass(frozen=True)
class SymbolResult:
    symbol: str
    rows_by_base: dict[str, list[WfRow]]


def _evaluate_one_sl(window, base_df, symbol, point, sl_mult, rates) -> WfRow:
    closes = base_df["close"].to_numpy(dtype=np.float64)
    highs = base_df["high"].to_numpy(dtype=np.float64)
    lows = base_df["low"].to_numpy(dtype=np.float64)
    n_bars = len(base_df)
    emitted = window.times_ms.size
    start = max(0, n_bars - emitted)

    atr_full = indicators.atr(highs[None, :], lows[None, :], closes[None, :],
                              config.ATR_PERIOD)[0]
    atr = np.nan_to_num(atr_full[start:], nan=0.0)

    closes_w = closes[start:]
    highs_w = highs[start:]
    lows_w = lows[start:]

    bt_trades = pair_trades_with_sl(window.triggers, closes_w, highs_w, lows_w,
                                    atr, sl_mult)
    # times_ms is window-aligned and per-bar; convert to ns for the swap calc.
    times_ns = window.times_ms.astype(np.int64) * 1_000_000
    trades = apply_swap_costs(bt_trades, symbol, times_ns, closes_w, rates)

    spread_pts = (base_df["spread"].to_numpy(dtype=np.float64)[start:]
                  if "spread" in base_df.columns
                  else np.zeros(emitted, dtype=np.float64))
    adx_2d, _, _ = indicators.adx(highs[None, :], lows[None, :], closes[None, :],
                                  config.ADX_PERIOD)
    adx = np.nan_to_num(adx_2d[0][start:], nan=0.0)

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


def run_symbol(symbol: str, rates: pd.DataFrame) -> SymbolResult | None:
    log = logging.getLogger("backtest_swap")
    t0 = time.perf_counter()
    frames = load_symbol(symbol)
    point = POINT_BY_SYMBOL[symbol]
    emit_window = max(len(df) for df in frames.values()) + 100

    result = dws_smt.compute_symbol(frames, out_bars=emit_window)
    if result is None:
        log.error("%s: dws_smt.compute_symbol returned None", symbol)
        return None

    rows_by_base: dict[str, list[WfRow]] = {}
    for base_tf in config.DWS_SMT_BASE_TFS:
        window = result.by_base.get(base_tf)
        base_df = frames.get(base_tf)
        if window is None or base_df is None:
            continue
        rows_by_base[base_tf] = [
            _evaluate_one_sl(window, base_df, symbol, point, m, rates)
            for m in SL_GRID
        ]
    log.info("%-7s done in %.1fs", symbol, time.perf_counter() - t0)
    del frames, result
    gc.collect()
    return SymbolResult(symbol=symbol, rows_by_base=rows_by_base)


# --------------------------------------------------------------------------- #
# Reporting — same shape as the no-swap script for easy A/B comparison
# --------------------------------------------------------------------------- #

def _fmt_sl(m: float) -> str:
    return "∞" if not np.isfinite(m) else f"{m:.1f}"


def _pct(x: float) -> str:
    return f"{x * 100:5.1f}%"


def _fmt_pf(pf: float) -> str:
    return "   ∞" if pf == float("inf") else f"{pf:>5.2f}"


def _verdict(rows: list[WfRow]) -> tuple[str, WfRow, WfRow]:
    baseline = next(r for r in rows if not np.isfinite(r.sl_mult))
    finite = [r for r in rows if np.isfinite(r.sl_mult)]
    if not finite:
        return ("NO-SL-CANDIDATES", baseline, baseline)
    train_best = max(finite, key=lambda r: r.train.profit_factor)
    if baseline.test.profit_factor >= train_best.test.profit_factor:
        return ("SIGNAL-OUT BEST", baseline, train_best)
    deg = ((train_best.train.profit_factor - train_best.test.profit_factor)
           / max(train_best.train.profit_factor, 1e-9))
    if deg < 0.20:
        return ("SL HELPS ROBUST", baseline, train_best)
    return ("SL HELPS BUT DEGRADES", baseline, train_best)


def print_symbol_detail(sym_res: SymbolResult) -> None:
    print(f"\n{'=' * 78}\n  {sym_res.symbol}  (with swap costs)\n{'=' * 78}")
    for base_tf, rows in sym_res.rows_by_base.items():
        stack = " / ".join(config.DWS_SMT_STACKS[base_tf])
        print(f"\n  BASE = {base_tf}  (stack: {stack})")
        print(f"    {'SL':<6}  {'N(train)':>8}  {'PF(train)':>9}  {'PF(test)':>8}  "
              f"{'exp(test)':>10}  {'DD(test)':>9}  tier(test)")
        for r in rows:
            print(f"    {_fmt_sl(r.sl_mult):<6}  "
                  f"{r.train.n_trades:>8,d}  "
                  f"{_fmt_pf(r.train.profit_factor):>9}  "
                  f"{_fmt_pf(r.test.profit_factor):>8}  "
                  f"{r.test.expectancy:>+10.1f}  "
                  f"{r.test.max_drawdown:>9.0f}  "
                  f"{r.test.tier}")
        verdict, baseline, best = _verdict(rows)
        if verdict == "SIGNAL-OUT BEST":
            print(f"    → {verdict}  (signal-out PF={_fmt_pf(baseline.test.profit_factor)} "
                  f">  best finite SL PF={_fmt_pf(best.test.profit_factor)})")
        else:
            print(f"    → {verdict}  (train-best SL={best.sl_mult}×ATR  "
                  f"test PF={_fmt_pf(best.test.profit_factor)}  "
                  f"vs baseline {_fmt_pf(baseline.test.profit_factor)})")


def print_consolidated(symbol_results: list[SymbolResult]) -> None:
    print()
    print("=" * 78)
    print(" CONSOLIDATED (WITH SWAP)  —  Best SL strategy per (symbol, base TF)")
    print("=" * 78)
    print(f"  {'Symbol':<8}  {'Base':<4}  {'Baseline PF':>11}  {'Best-train SL':>14}  "
          f"{'Best-SL test PF':>16}  Verdict")
    print(f"  {'-' * 8}  {'-' * 4}  {'-' * 11}  {'-' * 14}  {'-' * 16}  {'-' * 24}")
    for sr in symbol_results:
        for base_tf in config.DWS_SMT_BASE_TFS:
            rows = sr.rows_by_base.get(base_tf)
            if rows is None:
                continue
            verdict, baseline, best = _verdict(rows)
            print(f"  {sr.symbol:<8}  {base_tf:<4}  "
                  f"{_fmt_pf(baseline.test.profit_factor):>11}  "
                  f"{_fmt_sl(best.sl_mult)+'×ATR':>14}  "
                  f"{_fmt_pf(best.test.profit_factor):>16}  {verdict}")
    counts: dict[str, int] = {}
    for sr in symbol_results:
        for rows in sr.rows_by_base.values():
            v, _, _ = _verdict(rows)
            counts[v] = counts.get(v, 0) + 1
    print()
    print("  Verdict distribution across 10 symbols × 3 base TFs = 30 cells:")
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"    {v:>3}  {k}")


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
    log = logging.getLogger("backtest_swap")

    rates = load_rates()
    log.info("Loaded rates: %d daily rows, %s → %s, currencies %s",
             len(rates),
             rates.index.min().date(), rates.index.max().date(),
             list(rates.columns))

    symbols = [s.base for s in config.SYMBOLS]
    log.info("Running SL walk-forward + swap on %d symbols", len(symbols))

    t_total = time.perf_counter()
    results: list[SymbolResult] = []
    for sym in symbols:
        if sym not in POINT_BY_SYMBOL:
            log.warning("No point size configured for %s — skipping", sym)
            continue
        try:
            res = run_symbol(sym, rates)
        except FileNotFoundError as e:
            log.warning("%s: CSV missing — skipping (%s)", sym, e)
            continue
        if res is not None:
            results.append(res)
    log.info("All symbols done in %.1fs", time.perf_counter() - t_total)

    for sr in results:
        print_symbol_detail(sr)

    print_consolidated(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
