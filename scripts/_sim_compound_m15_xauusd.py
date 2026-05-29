"""XAUUSD M15 COMPOUNDING study — 100k JPY start, 2010-2025, leverage 1000x.

Same production DWS-SMT signal + 16Y OOS ledger as the fixed-0.01 sim, but here
position size GROWS with equity. The point is to find the *optimal* compounding
fraction and show the growth/drawdown trade-off honestly.

Money-management model
----------------------
The system has no fixed monetary stop (exits are signal-based), so the natural
sizing law is FIXED-FRACTIONAL EXPOSURE: lots proportional to current equity.

    lots_i = g * equity_i           (g = lots per JPY of equity)
    equity_{i+1} = equity_i * (1 + g * pnl_per_lot_i)

where pnl_per_lot_i = net_pts_i * point * contract_oz * USDJPY_i is the JPY P/L
of ONE standard lot for trade i (net of the Dukascopy bid-ask spread).

Anchor: "0.01 lot at 100,000 JPY" => g0 = 0.01 / 100000 lots/JPY. We sweep a
multiple m so g = m * g0; m = 1 means start exactly at 0.01 lot and let it scale
up with the account (the conservative proportional baseline).

Two risk ceilings are computed honestly:
  * closed-loss ruin:  g < 1 / max(-pnl_per_lot)      (worst settled loss)
  * intra-trade ruin:  g < 1 / max(mae_per_lot)       (worst floating excursion;
    this is the BINDING constraint — a small account is stopped out mid-trade).

Kelly: m* maximises terminal log-wealth Sigma log(1 + g*pnl_per_lot_i). Full
Kelly is growth-optimal but its drawdown is brutal and it is fragile to
estimation error, so the practical recommendation is a FRACTION of it.

Finally a REALISTIC broker-quantised "lot ladder" is simulated (lots rounded
DOWN to 0.01, min 0.01) at the recommended fraction, with a full yearly table.

Run:  py scripts/_sim_compound_m15_xauusd.py
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
CONTRACT_OZ = 100.0
LEVERAGE = 1000.0
Y0, Y1 = 2010, 2025
N_YEARS = Y1 - Y0 + 1
G0 = 0.01 / INITIAL_JPY             # lots per JPY: "0.01 lot @ 100k"


def _usdjpy_daily():
    df = oos._load_tf("USDJPY", "D1", oos.POINT_BY_SYMBOL["USDJPY"])
    return (df.index.values.astype("int64") // 1_000_000).astype("int64"), \
        df["close"].to_numpy(dtype=np.float64)


def _build_ledger():
    """Return per-trade arrays aligned chronologically: pnl_per_lot (JPY),
    mae_per_lot (JPY), year, and entry epoch-ms."""
    point = oos.POINT_BY_SYMBOL[SYMBOL]
    frames = {tf: oos._load_tf(SYMBOL, tf, point)
              for tf in ("W1", "D1", "H4", "H1", "M15")}
    emit = max(len(d) for d in frames.values()) + 100
    result = dws_smt.compute_symbol(
        frames=frames, stacks=config.DWS_SMT_STACKS,
        period=config.DWS_SMT_PERIOD, smooth=config.DWS_SMT_SMOOTH,
        out_bars=emit,
    )
    rows = oos._build_trade_rows(result.by_base[BASE], frames[BASE], point)
    rows = [r for r in rows if Y0 <= r.entry_year <= Y1]
    rows.sort(key=lambda r: r.entry_ms)

    fx_t, fx_r = _usdjpy_daily()

    def rate_at(ms: int) -> float:
        k = int(np.searchsorted(fx_t, ms, side="right")) - 1
        return float(fx_r[max(0, k)])

    per_lot = point * CONTRACT_OZ                      # JPY-per-(pt*rate) for 1 lot
    pnl, mae, yr = [], [], []
    for r in rows:
        rate = rate_at(r.entry_ms)
        pnl.append(r.net_pts * per_lot * rate)         # JPY P/L per 1.0 lot
        mae.append(abs(r.mae_pts) * per_lot * rate)    # JPY adverse swing per lot
        yr.append(r.entry_year)
    gmax = float(np.nanmax(frames[BASE]["close"].to_numpy(dtype=np.float64)))
    return (np.array(pnl), np.array(mae), np.array(yr),
            gmax, fx_r.max())


def _sim_proportional(pnl, mae, g, quantize=False):
    """Sequential fixed-fractional account. Returns dict of stats.

    quantize=False: continuous lots (theoretical, for the Kelly sweep).
    quantize=True : lots floored to 0.01 (min 0.01) — realistic broker ladder.
    """
    eq = INITIAL_JPY
    peak = eq
    max_dd_pct = 0.0
    ruin = False
    margin_call = False
    for p, m in zip(pnl, mae):
        lots = g * eq
        if quantize:
            lots = max(0.01, np.floor(lots / 0.01) * 0.01)
        # Intra-trade floating low — stop-out if it wipes equity.
        if eq - lots * m <= 0.0:
            margin_call = True
        eq += lots * p
        if eq <= 0.0:
            ruin = True
            eq = 0.0
            break
        peak = max(peak, eq)
        max_dd_pct = max(max_dd_pct, (peak - eq) / peak * 100.0)
    cagr = ((eq / INITIAL_JPY) ** (1.0 / N_YEARS) - 1.0) * 100.0 if eq > 0 else float("nan")
    return {"final": eq, "cagr": cagr, "max_dd_pct": max_dd_pct,
            "ruin": ruin, "margin_call": margin_call}


def _kelly_multiple(pnl):
    """m* (in units of G0) maximising terminal log-wealth, subject to survival."""
    worst = float((-pnl).max())                         # worst settled loss / lot
    g_cap = 1.0 / worst if worst > 0 else np.inf        # 1 + g*pnl > 0 for all
    # Coarse-to-fine search on g in (0, ~0.95*g_cap).
    best_m, best_log = 0.0, -np.inf
    grid = np.linspace(1e-9, g_cap * 0.98, 4000)
    for g in grid:
        s = np.log1p(g * pnl)
        if not np.all(np.isfinite(s)):
            continue
        tot = s.sum()
        if tot > best_log:
            best_log, best_m = tot, g
    return best_m / G0, g_cap / G0


def _yearly_ladder(pnl, mae, yr, g, cap=np.inf):
    """Realistic quantised ladder run with a per-year breakdown."""
    eq = INITIAL_JPY
    out = {}
    for p, m, y in zip(pnl, mae, yr):
        lots = max(0.01, min(cap, np.floor((g * eq) / 0.01) * 0.01))
        if y not in out:
            out[y] = {"n": 0, "wins": 0, "pnl": 0.0, "peak": eq,
                      "ydd": 0.0, "lots_lo": lots, "lots_hi": lots}
        d = out[y]
        before = eq
        eq += lots * p
        d["n"] += 1
        d["pnl"] += eq - before
        if p > 0:
            d["wins"] += 1
        d["peak"] = max(d["peak"], eq)
        d["ydd"] = max(d["ydd"], d["peak"] - eq)
        d["lots_lo"] = min(d["lots_lo"], lots)
        d["lots_hi"] = max(d["lots_hi"], lots)
        d["end"] = eq
    return eq, out


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    pnl, mae, yr, gmax, fx_hi = _build_ledger()
    n = pnl.size
    worst_loss = float((-pnl).max())
    worst_mae = float(mae.max())
    g_loss_cap = 1.0 / worst_loss
    g_mae_cap = 1.0 / worst_mae
    m_kelly, m_loss_cap = _kelly_multiple(pnl)
    m_mae_cap = g_mae_cap / G0

    P = print
    P("=" * 78)
    P(f" XAUUSD M15 COMPOUNDING study | {Y0}-{Y1} | start 0.01 lot @ 100k | lev 1:{int(LEVERAGE)}")
    P("=" * 78)
    P(f" Trades={n}   worst settled loss/lot={worst_loss:,.0f} JPY   "
      f"worst MAE/lot={worst_mae:,.0f} JPY")
    P(f" Survival caps (multiple of base 0.01@100k):")
    P(f"   closed-loss ruin cap : m < {m_loss_cap:,.1f}")
    P(f"   intra-trade(MAE) cap : m < {m_mae_cap:,.1f}   <-- BINDING real-world ruin limit")
    P(f" Growth-optimal (full Kelly, closed-loss basis): m* = {m_kelly:,.1f}")
    P("-" * 78)
    P(" Fixed-fractional sweep (continuous lots) — multiple m of base 0.01@100k:")
    P(f" {'m':>7}{'lots@end~':>12}{'FinalJPY':>16}{'CAGR%':>8}{'MaxDD%':>9}{'Ruin':>6}{'MgnCall':>8}")
    P("-" * 78)
    # A spread of fractions incl. fractions of Kelly and the MAE cap.
    candidate_m = sorted({1, 2, 5, 10, 20, 50,
                          round(m_kelly * 0.25, 1), round(m_kelly * 0.5, 1),
                          round(m_kelly, 1), round(m_mae_cap * 0.5, 1)})
    rec = None
    for m in candidate_m:
        if m <= 0:
            continue
        g = m * G0
        s = _sim_proportional(pnl, mae, g, quantize=False)
        lots_end = g * s["final"] if s["final"] > 0 else 0.0
        P(f" {m:>7.1f}{lots_end:>12.2f}{s['final']:>16,.0f}{s['cagr']:>8.1f}"
          f"{s['max_dd_pct']:>9.1f}{('YES' if s['ruin'] else 'no'):>6}"
          f"{('YES' if s['margin_call'] else 'no'):>8}")
    P("=" * 78)

    P(" ^ UNCONSTRAINED compounding is a FANTASY: even m=1 ends at ~8.7M lots —")
    P("   physically un-fillable. Real terminal wealth is set by the LOT CEILING")
    P("   (broker max lot + liquidity), NOT by Kelly. So cap the lot and choose")
    P("   the fraction by DRAWDOWN tolerance, not by growth.")
    P("=" * 78)

    # ----- Realistic: fixed-fractional, choose fraction by DD, CAP the lot. ----
    # Method: lots = floor(equity / step) * 0.01, capped at a broker/liquidity
    # max lot. step=100k -> "0.01 lot per 100,000 JPY of equity" (m=1, starts at
    # exactly 0.01@100k). step=50k -> m=2 (twice as aggressive).
    P(" REALISTIC fixed-fractional + lot ceiling (the practical optimum):")
    P(f" {'rule':<26}{'maxlot':>7}{'FinalJPY':>20}{'CAGR%':>8}{'MaxDD%':>9}")
    P("-" * 78)
    schemes = [("0.01 / 100k  (m=1)", 1.0),
               ("0.01 / 50k   (m=2)", 2.0)]
    rec_run = None
    for label, m in schemes:
        g = m * G0
        for cap in (1.0, 10.0, 50.0):
            final_c, dd_c = _sim_capped(pnl, mae, g, cap)
            cg = ((final_c / INITIAL_JPY) ** (1.0 / N_YEARS) - 1.0) * 100.0
            P(f" {label:<26}{cap:>7.0f}{final_c:>20,.0f}{cg:>8.1f}{dd_c:>9.1f}")
            if m == 1.0 and cap == 10.0:
                rec_run = g, cap
    P("=" * 78)

    # Yearly breakdown for the recommended rule (0.01/100k, cap 10 lots).
    g_rec, cap_rec = rec_run
    final_q, yearly = _yearly_ladder(pnl, mae, yr, g_rec, cap=cap_rec)
    cagr_q = ((final_q / INITIAL_JPY) ** (1.0 / N_YEARS) - 1.0) * 100.0
    P(f" RECOMMENDED yearly: 0.01 lot per 100,000 JPY, max {cap_rec:.0f} lots  "
      f"(final={final_q:,.0f} JPY, CAGR={cagr_q:.1f}%)")
    P(f" {'Year':<6}{'N':>6}{'Win%':>7}{'lots':>13}{'PnL(JPY)':>16}{'EndBal':>17}{'YrDD%':>8}")
    P("-" * 78)
    for y in range(Y0, Y1 + 1):
        if y not in yearly:
            continue
        d = yearly[y]
        wr = d["wins"] / d["n"] * 100
        lots_rng = (f"{d['lots_lo']:.2f}" if d['lots_lo'] == d['lots_hi']
                    else f"{d['lots_lo']:.2f}-{d['lots_hi']:.2f}")
        ydd_pct = d["ydd"] / d["peak"] * 100 if d["peak"] else 0.0
        P(f" {y:<6}{d['n']:>6}{wr:>6.1f}%{lots_rng:>13}{d['pnl']:>+16,.0f}"
          f"{d['end']:>17,.0f}{ydd_pct:>7.1f}%")
    P("=" * 78)
    P(" How to read it: compounding helps ONLY until the lot ceiling is hit;")
    P(" after that it is linear again (fixed max lot). The fraction (m) sets your")
    P(" DRAWDOWN: m=1 -> ~15% peak DD, m=2 -> ~29%. Kelly (m~12) is growth-optimal")
    P(" but unbearable AND fragile (in-sample edge). Pick m by the DD you can sit")
    P(" through, cap the lot to what you can actually fill, never chase Kelly.")
    P(" Caveats: NO swap/financing, NO slippage; both worsen high-frequency M15.")
    P(" Past data; not a forecast.")
    return 0


def _sim_capped(pnl, mae, g, max_lot):
    """Fixed-fractional with lots floored to 0.01 and capped at *max_lot*.
    Returns (final_equity_JPY, max_drawdown_pct)."""
    eq = INITIAL_JPY
    peak = eq
    max_dd = 0.0
    for p, m in zip(pnl, mae):
        lots = max(0.01, min(max_lot, np.floor((g * eq) / 0.01) * 0.01))
        eq += lots * p
        if eq <= 0:
            return 0.0, 100.0
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak * 100.0)
    return eq, max_dd


if __name__ == "__main__":
    raise SystemExit(main())
