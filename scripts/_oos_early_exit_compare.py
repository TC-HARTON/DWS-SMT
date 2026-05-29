"""Early-EXIT vs baseline — 16Y XAUUSD comparison (option B, lower-TF rule).

Question: the H4 DWS-SMT closes a position only on a CONFIRMED H4 EXIT/opposite
(up to a 4 h lag). Does exiting EARLIER — when a LOWER timeframe (H1 / M15)
DWS-SMT fires EXIT or an opposite signal during the hold — improve the edge?

Rule (look-ahead-free, fully backtestable on bar data):
  * Entry: unchanged (H4 base trigger, entry = H4 entry-bar close).
  * baseline exit: the original confirmed H4 EXIT/opposite (current model).
  * early(L) exit: the FIRST confirmed L-bar (L in {H1, M15}) strictly between
    entry and the H4 exit whose trigger is EXIT or the opposite of the position.
    Fill = that L bar's close. Else fall back to the baseline exit.
  Cost (entry-bar spread) is identical across variants, so the only difference
  is WHERE the trade is closed — an apples-to-apples test of the exit rule.

Run:  py scripts/_oos_early_exit_compare.py
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
import _oos_xauusd_16y as oos       # noqa: E402  (reuse loaders + _aggregate)

SYMBOL = "XAUUSD"
BASE = "H4"
LOWERS = ("H1", "M15")

# Bar durations (ms) — used to convert a bar's OPEN time (what ``times_ms``
# holds) to its CLOSE (= fill) time, so an exit can never be timed before the
# entry actually fills.
_TF_MS = {"M15": 15 * 60_000, "H1": 60 * 60_000, "H4": 240 * 60_000,
          "D1": 1440 * 60_000, "W1": 10080 * 60_000}


def _win(result, frames, tf):
    """(times_ms int64, triggers list, close[], spread[]) aligned to the window."""
    w = result.by_base[tf]
    df = frames[tf]
    start = max(0, len(df) - w.times_ms.size)
    close = df["close"].to_numpy(dtype=np.float64)[start:]
    spread = (df["spread"].to_numpy(dtype=np.float64)[start:]
              if "spread" in df.columns else np.zeros(w.times_ms.size))
    return w.times_ms.astype("int64"), list(w.triggers), close, spread


def _repair(trigs):
    """Re-pair closed trades from a trigger array → (entry_idx, exit_idx, dir).

    Mirrors dws_smt._pair_trades: BUY/SELL opens or stop-and-reverses, EXIT
    closes; the trailing still-open position is dropped (matches is_open)."""
    out, pos_dir, pos_entry = [], 0, -1
    for j, g in enumerate(trigs):
        if g in ("BUY", "SELL"):
            nd = 1 if g == "BUY" else -1
            if pos_dir not in (0, nd):
                out.append((pos_entry, j, pos_dir))     # reversal closes here
            if pos_dir != nd:
                pos_dir, pos_entry = nd, j
        elif g == "EXIT" and pos_dir != 0:
            out.append((pos_entry, j, pos_dir))
            pos_dir = 0
    return out


def _stats(nets):
    a = oos._aggregate(nets, [0.0] * len(nets))
    pf = a["profit_factor"]
    return {
        "n": a["n"], "wr": a["win_rate"], "pf": pf,
        "exp": a["expectancy"], "dd": a["max_drawdown"], "tier": a["tier"],
    }


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
    if result is None:
        print("compute_symbol returned None")
        return 1

    h4_t, h4_g, h4_close, h4_spread = _win(result, frames, BASE)
    trades = _repair(h4_g)
    lower = {L: _win(result, frames, L)[:3] for L in LOWERS}  # (times, trigs, close)

    def net(exit_price, entry_price, d, cost):
        return (exit_price - entry_price) * d / point - cost

    baseline, hold_base = [], []
    early = {L: [] for L in LOWERS}
    hold_early = {L: [] for L in LOWERS}
    cut = {L: 0 for L in LOWERS}

    base_ms = _TF_MS[BASE]
    for ei, xi, d in trades:
        if ei < 0 or ei >= len(h4_close) or xi >= len(h4_close):
            continue
        et, ep = h4_t[ei], h4_close[ei]
        ot, op = h4_t[xi], h4_close[xi]
        cost = h4_spread[ei]
        # Fills happen at the bar CLOSE, not its OPEN: entry fills at the H4
        # entry-bar close (et + base_ms); the baseline exit at the H4 exit-bar
        # close (ot + base_ms).
        entry_fill_t = et + base_ms
        base_exit_fill_t = ot + base_ms
        baseline.append(net(op, ep, d, cost))
        hold_base.append((base_exit_fill_t - entry_fill_t) / 3_600_000)   # hours
        opp = "BUY" if d == -1 else "SELL"
        for L in LOWERS:
            lt, lg, lc = lower[L]
            l_ms = _TF_MS[L]
            ex_p, ex_fill_t = op, base_exit_fill_t
            # Eligible lower bars must OPEN at/after the entry fill. A lower bar
            # opening INSIDE the H4 entry bar would close (= fill) before the
            # position is even entered — a look-ahead exit that unfairly favours
            # the early-exit variant. Its own fill is at its close (lt[k]+l_ms).
            k = int(np.searchsorted(lt, entry_fill_t, side="left"))
            while k < len(lt) and lt[k] < ot:
                if lg[k] in ("EXIT", opp):
                    ex_p, ex_fill_t = lc[k], lt[k] + l_ms
                    cut[L] += 1
                    break
                k += 1
            early[L].append(net(ex_p, ep, d, cost))
            hold_early[L].append((ex_fill_t - entry_fill_t) / 3_600_000)

    def line(name, nets, holds):
        s = _stats(nets)
        pf = "inf" if s["pf"] is None else f"{s['pf']:.2f}"
        return (f"{name:14s} N={s['n']:4d}  WR={s['wr']*100:5.1f}%  PF={pf:>5s}  "
                f"exp={s['exp']:+8.1f}pt  MaxDD={s['dd']:9.0f}pt  "
                f"avgHold={np.mean(holds):5.1f}h  tier={s['tier']}")

    print(f"\n=== XAUUSD H4 base - early-EXIT comparison (16Y, N_trades={len(baseline)}) ===")
    print(line("baseline(H4)", baseline, hold_base))
    for L in LOWERS:
        print(line(f"early({L})", early[L], hold_early[L])
              + f"  cut_early={cut[L]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
