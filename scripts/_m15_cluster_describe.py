"""For each of the 4 walk-forward clusters, report:
  (a) original-direction WR + reverse-direction WR (after re-paying spread)
  (b) centroid feature profile in plain words (MTF + indicator commonalities)
"""
from __future__ import annotations
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.preprocessing import StandardScaler
from sklearn.cluster import KMeans

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyzer import dws_smt, indicators
import config
from scripts._backtest_xauusd import _load_tf, XAUUSD_POINT


def build_tf(df):
    close = df["close"].to_numpy(dtype=np.float64)
    high  = df["high"].to_numpy(dtype=np.float64)
    low   = df["low"].to_numpy(dtype=np.float64)
    ema20 = indicators.ema(close, 20)
    ema50 = indicators.ema(close, 50)
    rsi14 = indicators.rsi(close, 14)
    atr14 = indicators.atr(high, low, close, 14)
    adx14, di_p, di_m = indicators.adx(high, low, close, 14)
    ns = df.index.values.astype("datetime64[ns]").astype("int64")
    return dict(close=close, high=high, low=low, ema20=ema20, ema50=ema50,
                rsi=rsi14, atr=atr14, adx=adx14, dip=di_p, dim=di_m, ns=ns)


def at_or_before(arr_ns, target):
    pos = int(np.searchsorted(arr_ns, target, side="right") - 1)
    return pos if pos >= 0 else None


def last_closed(arr_ns, target):
    idx = at_or_before(arr_ns, target)
    return idx - 1 if idx is not None and idx >= 1 else None


def safe(v):
    if v is None: return 0.0
    fv = float(v)
    if math.isnan(fv) or math.isinf(fv): return 0.0
    return fv


def main():
    SLOPE_LOOKBACK = 5
    frames = {tf: _load_tf(tf, XAUUSD_POINT) for tf in ("W1","D1","H4","H1","M15")}
    result = dws_smt.compute_symbol(
        frames=frames, stacks=config.DWS_SMT_STACKS,
        period=config.DWS_SMT_PERIOD, smooth=config.DWS_SMT_SMOOTH,
        out_bars=max(len(df) for df in frames.values()) + 100,
    )
    w = result.by_base["M15"]
    tf_h4  = build_tf(frames["H4"])
    tf_h1  = build_tf(frames["H1"])
    tf_m15 = build_tf(frames["M15"])
    spread_pts = frames["M15"]["spread"].to_numpy(dtype=np.int64)
    base_ns = tf_m15["ns"]
    rows = []
    for tr in w.trades:
        if tr.is_open or tr.entry_idx >= base_ns.size: continue
        entry_ns = int(base_ns[tr.entry_idx])
        d = tr.direction
        bi = at_or_before(base_ns, entry_ns)
        mi = last_closed(tf_h1["ns"], entry_ns)
        ti = last_closed(tf_h4["ns"], entry_ns)
        if bi is None or mi is None or ti is None or bi < SLOPE_LOOKBACK + 1: continue
        atr_b = tf_m15["atr"][bi]
        sl_end = tf_m15["close"][bi]
        sl_beg = tf_m15["close"][bi - SLOPE_LOOKBACK]
        slope = ((sl_end - sl_beg) / atr_b) * d if atr_b and atr_b > 0 else 0.0
        bars_per_day = 96
        atr_hist = tf_m15["atr"][max(0, bi - bars_per_day*90): bi+1]
        atr_hist = atr_hist[~np.isnan(atr_hist)]
        atr_pct_90d = float((atr_hist < atr_b).mean()) if atr_hist.size > 5 and atr_b > 0 else 0.5
        entry_ts = pd.Timestamp(entry_ns, unit="ns", tz="UTC").tz_convert("Asia/Tokyo")
        cost = float(spread_pts[tr.entry_idx]) if tr.entry_idx < spread_pts.size else 0.0
        gross_pts = tr.points / XAUUSD_POINT      # already direction-signed
        net = gross_pts - cost
        # If we reverse direction: gross_rev = -gross_pts; new net = -gross_pts - cost
        net_rev = -gross_pts - cost
        rows.append({
            "year": entry_ts.year,
            "outcome":     "win" if net      > 0 else "loss",
            "outcome_rev": "win" if net_rev  > 0 else "loss",
            "net_pts": net, "net_rev_pts": net_rev, "spread_cost": cost,
            "direction": d,
            "base_rsi": safe(tf_m15["rsi"][bi]),
            "base_adx": safe(tf_m15["adx"][bi]),
            "base_di_diff": safe(tf_m15["dip"][bi] - tf_m15["dim"][bi]) * d,
            "base_atr_pct": safe(atr_b / tf_m15["close"][bi] * 100.0) if tf_m15["close"][bi] else 0.0,
            "base_ema_dist": safe((tf_m15["close"][bi] - tf_m15["ema20"][bi]) / atr_b) * d if atr_b > 0 else 0.0,
            "base_ema_slope": safe(slope),
            "base_close_vs_ema50": safe(tf_m15["close"][bi] - tf_m15["ema50"][bi]) * d,
            "mid_rsi": safe(tf_h1["rsi"][mi]),
            "mid_adx": safe(tf_h1["adx"][mi]),
            "mid_di_diff": safe(tf_h1["dip"][mi] - tf_h1["dim"][mi]) * d,
            "mid_atr_pct": safe(tf_h1["atr"][mi] / tf_h1["close"][mi] * 100.0) if tf_h1["close"][mi] else 0.0,
            "mid_ema_dist": safe((tf_h1["close"][mi] - tf_h1["ema20"][mi]) / tf_h1["atr"][mi]) * d if tf_h1["atr"][mi] > 0 else 0.0,
            "mid_close_vs_ema50": safe(tf_h1["close"][mi] - tf_h1["ema50"][mi]) * d,
            "top_rsi": safe(tf_h4["rsi"][ti]),
            "top_adx": safe(tf_h4["adx"][ti]),
            "top_di_diff": safe(tf_h4["dip"][ti] - tf_h4["dim"][ti]) * d,
            "top_ema_dist": safe((tf_h4["close"][ti] - tf_h4["ema20"][ti]) / tf_h4["atr"][ti]) * d if tf_h4["atr"][ti] > 0 else 0.0,
            "top_close_vs_ema50": safe(tf_h4["close"][ti] - tf_h4["ema50"][ti]) * d,
            "hour_jst": float(entry_ts.hour),
            "dow": float(entry_ts.dayofweek),
            "atr_pct_90d": atr_pct_90d,
        })
    df = pd.DataFrame(rows)
    FEATS = [c for c in df.columns if c not in ("year","outcome","outcome_rev","net_pts","net_rev_pts","spread_cost","direction")]

    train = df[df.year <= 2020].reset_index(drop=True)
    test  = df[df.year >  2020].reset_index(drop=True)

    Xtr = train[FEATS].to_numpy(dtype=np.float64)
    scaler = StandardScaler().fit(Xtr)
    Xtr_z = scaler.transform(Xtr)
    train_wins = (train.outcome == "win").to_numpy()
    km = KMeans(n_clusters=4, n_init=10, random_state=42).fit(Xtr_z[train_wins])
    cents_z = km.cluster_centers_

    def assign(X_raw):
        return ((scaler.transform(X_raw)[:, None, :] - cents_z[None, :, :])**2).sum(axis=2).argmin(axis=1)

    test_assign = assign(test[FEATS].to_numpy(dtype=np.float64))

    print("="*88)
    print("Cluster outcomes — original direction vs DIRECTION-REVERSED (test 2021-2025)")
    print("="*88)
    print(f"{'#':<3s} {'N':>5s}  {'orig WR':>8s}  {'rev WR':>8s}  {'rev edge*':>10s}  {'mean_net':>9s}  {'mean_rev':>9s}  {'avg_spread':>10s}")
    print("-"*88)
    print("* rev edge = rev_WR − orig_WR. >0 means reversing gives more wins; net edge still depends on spread.")
    for ci in range(4):
        m = test_assign == ci
        n = int(m.sum())
        sub = test[m]
        orig_wr = (sub.outcome == "win").mean()
        rev_wr  = (sub.outcome_rev == "win").mean()
        edge = (rev_wr - orig_wr) * 100
        mean_net = sub.net_pts.mean()
        mean_rev = sub.net_rev_pts.mean()
        avg_spr = sub.spread_cost.mean()
        print(f"{ci+1:<3d} {n:>5,d}  {orig_wr*100:>7.2f}%  {rev_wr*100:>7.2f}%  {edge:>+9.2f}pp  "
              f"{mean_net:>+8.0f}  {mean_rev:>+8.0f}  {avg_spr:>10.1f}")

    print()
    print("="*88)
    print("Cluster feature profiles (centroid in raw, direction-signed units)")
    print("="*88)
    raw_cents = scaler.inverse_transform(cents_z)
    print(f"{'feature':<24s} " + " ".join([f"  C#{i+1:<5d}" for i in range(4)]))
    print("-"*88)
    for j, f in enumerate(FEATS):
        vals = " ".join([f"  {raw_cents[i, j]:+8.2f}" for i in range(4)])
        print(f"{f:<24s} {vals}")


if __name__ == "__main__":
    main()
