"""XAUUSD M15 account simulation — 100k JPY, 0.01 lot, leverage 1000x, 2010-2025.

Reuses the production deterministic DWS-SMT signal and the exact 16Y OOS trade
ledger (M15 base, ``_oos_xauusd_16y._build_trade_rows``), then runs a SEQUENTIAL
fixed-size account so drawdown and ruin are realistic (not just summed points):

  * Each closed trade's net points already include the Dukascopy bid-ask spread.
  * P/L(USD) = net_pts * point * (contract_size * lots).  XAUUSD: 1 lot = 100 oz,
    point = 0.001, lots = 0.01  ->  units = 1 oz, so P/L(USD) = net_pts * 0.001.
  * P/L(JPY) = P/L(USD) * USDJPY at the trade's entry day (Dukascopy USDJPY D1).
  * Balance starts at 100,000 JPY; lot is fixed 0.01 (no compounding).
  * Ruin / margin call is checked against the worst intra-trade floating equity
    (using each trade's MAE), since a small account can be stopped out mid-trade.

Swap/financing and slippage are NOT modelled (mirrors the OOS baseline).

Run:  py scripts/_sim_account_m15_xauusd.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config                       # noqa: E402
from analyzer import dws_smt        # noqa: E402
import _oos_xauusd_16y as oos       # noqa: E402

SYMBOL = "XAUUSD"
BASE = "M15"
INITIAL_JPY = 100_000.0
LOTS = 0.01
CONTRACT_OZ = 100.0                 # oz per standard XAUUSD lot
LEVERAGE = 1000.0
UNITS = CONTRACT_OZ * LOTS          # 1.0 oz actually traded at 0.01 lot
Y0, Y1 = 2010, 2025


def _usdjpy_daily():
    """(epoch_ms[], close[]) for USDJPY D1 — the USD->JPY conversion series."""
    df = oos._load_tf("USDJPY", "D1", oos.POINT_BY_SYMBOL["USDJPY"])
    t = (df.index.values.astype("int64") // 1_000_000).astype("int64")
    return t, df["close"].to_numpy(dtype=np.float64)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    point = oos.POINT_BY_SYMBOL[SYMBOL]
    frames = {tf: oos._load_tf(SYMBOL, tf, point)
              for tf in ("W1", "D1", "H4", "H1", "M15")}
    emit = max(len(d) for d in frames.values()) + 100
    result = dws_smt.compute_symbol(
        frames=frames, stacks=config.DWS_SMT_STACKS,
        period=config.DWS_SMT_PERIOD, smooth=config.DWS_SMT_SMOOTH,
        out_bars=emit,
    )
    if result is None or result.by_base.get(BASE) is None:
        print("compute_symbol produced no M15 window")
        return 1

    rows = oos._build_trade_rows(result.by_base[BASE], frames[BASE], point)
    rows = [r for r in rows if Y0 <= r.entry_year <= Y1]
    rows.sort(key=lambda r: r.entry_ms)
    if not rows:
        print("no trades")
        return 1

    fx_t, fx_r = _usdjpy_daily()

    def rate_at(ms: int) -> float:
        k = int(np.searchsorted(fx_t, ms, side="right")) - 1
        return float(fx_r[max(0, k)])

    usd_per_pt = point * UNITS         # USD per net point (= 0.001)

    bal = INITIAL_JPY
    peak = bal
    max_dd = 0.0
    max_dd_pct = 0.0
    max_dd_year = None
    ruin = None
    margin_call = None
    best = (-1e18, 0)
    worst = (1e18, 0)
    wins = 0
    gross_win_jpy = 0.0
    gross_loss_jpy = 0.0
    rate_lo, rate_hi = 1e18, 0.0

    years = {y: {"n": 0, "wins": 0, "pnl": 0.0,
                 "start": None, "end": bal, "peak": bal, "ydd": 0.0}
             for y in range(Y0, Y1 + 1)}

    for r in rows:
        rate = rate_at(r.entry_ms)
        rate_lo, rate_hi = min(rate_lo, rate), max(rate_hi, rate)
        pnl_jpy = r.net_pts * usd_per_pt * rate
        mae_jpy = abs(r.mae_pts) * usd_per_pt * rate     # adverse excursion (JPY)

        y = r.entry_year
        yd = years[y]
        if yd["start"] is None:
            yd["start"] = bal
            yd["peak"] = bal

        # Worst intra-trade floating equity — margin-call / stop-out check.
        if margin_call is None and (bal - mae_jpy) <= 0.0:
            margin_call = (y, bal, mae_jpy)

        bal += pnl_jpy
        yd["n"] += 1
        yd["pnl"] += pnl_jpy
        yd["end"] = bal
        yd["peak"] = max(yd["peak"], bal)
        yd["ydd"] = max(yd["ydd"], yd["peak"] - bal)   # running intra-year DD
        if r.net_pts > 0.0:
            wins += 1
            yd["wins"] += 1
            gross_win_jpy += pnl_jpy
        else:
            gross_loss_jpy += -pnl_jpy
        best = max(best, (pnl_jpy, y))
        worst = min(worst, (pnl_jpy, y))

        peak = max(peak, bal)
        dd = peak - bal
        if dd > max_dd:
            max_dd, max_dd_pct, max_dd_year = dd, dd / peak * 100.0, y
        if ruin is None and bal <= 0.0:
            ruin = (y, r.entry_ms)

    n = len(rows)
    pf = (gross_win_jpy / gross_loss_jpy) if gross_loss_jpy > 0 else float("inf")
    final = bal
    ret_pct = (final / INITIAL_JPY - 1.0) * 100.0
    n_years = Y1 - Y0 + 1
    cagr = ((final / INITIAL_JPY) ** (1.0 / n_years) - 1.0) * 100.0 if final > 0 else float("nan")

    # Margin sanity at 1000x: worst-case notional uses the peak gold price.
    gmax = float(np.nanmax(frames[BASE]["close"].to_numpy(dtype=np.float64)))
    worst_margin_jpy = (gmax * UNITS * rate_hi) / LEVERAGE

    P = print
    P("=" * 74)
    P(f" XAUUSD M15 account sim  |  {Y0}-{Y1}  |  0.01 lot fixed  |  lev 1:{int(LEVERAGE)}")
    P("=" * 74)
    P(f" Initial balance      : {INITIAL_JPY:>14,.0f} JPY")
    P(f" Final balance        : {final:>14,.0f} JPY")
    P(f" Net profit           : {final - INITIAL_JPY:>+14,.0f} JPY   ({ret_pct:+.1f} %)")
    P(f" CAGR (16y)           : {cagr:>13.2f} %")
    P("-" * 74)
    P(f" Trades               : {n:>6d}")
    P(f" Win rate             : {wins / n * 100:>6.2f} %   ({wins}W / {n - wins}L)")
    P(f" Profit factor        : {('inf' if pf == float('inf') else f'{pf:.3f}'):>6}")
    P(f" Expectancy / trade   : {(final - INITIAL_JPY) / n:>+10,.1f} JPY")
    P(f" Best / worst trade   : {best[0]:>+10,.0f} ({best[1]})  /  {worst[0]:>+10,.0f} ({worst[1]}) JPY")
    P("-" * 74)
    P(f" Max drawdown         : {max_dd:>14,.0f} JPY   ({max_dd_pct:.1f} % of peak, ~{max_dd_year})")
    P(f" Account ruin (<=0)   : {'NO' if ruin is None else f'YES @ {ruin[0]}'}")
    P(f" Margin call (intra)  : {'NO' if margin_call is None else f'YES @ {margin_call[0]}'}")
    P(f" USDJPY range used    : {rate_lo:.2f} - {rate_hi:.2f}")
    P(f" Worst-case margin    : ~{worst_margin_jpy:,.0f} JPY at 1:{int(LEVERAGE)} "
      f"(gold peak {gmax:,.0f}); negligible vs balance")
    P("=" * 74)
    P(f" {'Year':<6}{'Trades':>8}{'Win%':>8}{'PnL(JPY)':>14}{'EndBal(JPY)':>16}{'YrDD(JPY)':>13}")
    P("-" * 74)
    for y in range(Y0, Y1 + 1):
        yd = years[y]
        if yd["n"] == 0:
            continue
        wr = yd["wins"] / yd["n"] * 100
        P(f" {y:<6}{yd['n']:>8}{wr:>7.1f}%{yd['pnl']:>+14,.0f}{yd['end']:>16,.0f}{yd['ydd']:>13,.0f}")
    P("=" * 74)
    P(" Notes: net P/L includes Dukascopy bid-ask spread. Swap/financing and")
    P(" slippage NOT modelled. USD->JPY at each trade's entry-day USDJPY (D1).")
    P(" Fixed 0.01 lot (no compounding). Same signal/ledger as oos_baseline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
