"""Walk-forward validation for M15 trigger-only win-subpatterns.

Split: train 2010-2020, test 2021-2025.

  1. On train trades: cluster the WIN subset into k=4 (KMeans, rs=42).
  2. Apply train centroids to test trades — reassign each test trade
     to its nearest centroid in z-space (using TRAIN scaler).
  3. For each cluster, compute the test-period empirical WR and run a
     2-proportion z-test vs the test baseline. Apply Bonferroni
     correction (α / k = 0.05 / 4 = 0.0125) for the multiple-cluster
     comparison.
  4. Report drift: |train WR − test WR| per cluster. < 5 pp ≈ genuine
     pattern, ≥ 10 pp = overfit / selection bias.
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


def build_tf(df: pd.DataFrame) -> dict:
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
    if idx is None: return None
    return idx - 1 if idx >= 1 else None


def safe(v):
    if v is None: return 0.0
    fv = float(v)
    if math.isnan(fv) or math.isinf(fv): return 0.0
    return fv


def main() -> int:
    SLOPE_LOOKBACK = 5
    print("Loading CSVs...")
    frames = {tf: _load_tf(tf, XAUUSD_POINT) for tf in ("W1","D1","H4","H1","M15")}
    print("DWS-SMT compute (M15 base)...")
    result = dws_smt.compute_symbol(
        frames=frames, stacks=config.DWS_SMT_STACKS,
        period=config.DWS_SMT_PERIOD, smooth=config.DWS_SMT_SMOOTH,
        out_bars=max(len(df) for df in frames.values()) + 100,
    )
    w = result.by_base["M15"]
    tf_h4  = build_tf(frames["H4"])
    tf_h1  = build_tf(frames["H1"])
    tf_m15 = build_tf(frames["M15"])
    spread = frames["M15"]["spread"].to_numpy(dtype=np.int64)
    base_ns = tf_m15["ns"]

    rows = []
    for tr in w.trades:
        if tr.is_open or tr.entry_idx >= base_ns.size:
            continue
        entry_ns = int(base_ns[tr.entry_idx])
        direction = tr.direction
        bi = at_or_before(base_ns, entry_ns)
        mi = last_closed(tf_h1["ns"], entry_ns)
        ti = last_closed(tf_h4["ns"], entry_ns)
        if bi is None or mi is None or ti is None or bi < SLOPE_LOOKBACK + 1:
            continue
        atr_b = tf_m15["atr"][bi]
        sl_end = tf_m15["close"][bi]
        sl_beg = tf_m15["close"][bi - SLOPE_LOOKBACK]
        slope = ((sl_end - sl_beg) / atr_b) * direction if atr_b and atr_b > 0 else 0.0
        bars_per_day = 96
        window = bars_per_day * 90
        lo = max(0, bi - window)
        atr_hist = tf_m15["atr"][lo: bi + 1]
        atr_hist = atr_hist[~np.isnan(atr_hist)]
        atr_pct_90d = float((atr_hist < atr_b).mean()) if atr_hist.size > 5 and atr_b > 0 else 0.5
        entry_ts = pd.Timestamp(entry_ns, unit="ns", tz="UTC").tz_convert("Asia/Tokyo")
        cost = float(spread[tr.entry_idx]) if tr.entry_idx < spread.size else 0.0
        net = tr.points / XAUUSD_POINT - cost
        rows.append({
            "year": entry_ts.year,
            "outcome": "win" if net > 0 else "loss",
            "net_pts": net,
            "base_rsi": safe(tf_m15["rsi"][bi]),
            "base_adx": safe(tf_m15["adx"][bi]),
            "base_di_diff": safe(tf_m15["dip"][bi] - tf_m15["dim"][bi]) * direction,
            "base_atr_pct": safe(atr_b / tf_m15["close"][bi] * 100.0) if tf_m15["close"][bi] else 0.0,
            "base_ema_dist": safe((tf_m15["close"][bi] - tf_m15["ema20"][bi]) / atr_b) * direction if atr_b > 0 else 0.0,
            "base_ema_slope": safe(slope),
            "base_close_vs_ema50": safe(tf_m15["close"][bi] - tf_m15["ema50"][bi]) * direction,
            "mid_rsi": safe(tf_h1["rsi"][mi]),
            "mid_adx": safe(tf_h1["adx"][mi]),
            "mid_di_diff": safe(tf_h1["dip"][mi] - tf_h1["dim"][mi]) * direction,
            "mid_atr_pct": safe(tf_h1["atr"][mi] / tf_h1["close"][mi] * 100.0) if tf_h1["close"][mi] else 0.0,
            "mid_ema_dist": safe((tf_h1["close"][mi] - tf_h1["ema20"][mi]) / tf_h1["atr"][mi]) * direction if tf_h1["atr"][mi] > 0 else 0.0,
            "mid_close_vs_ema50": safe(tf_h1["close"][mi] - tf_h1["ema50"][mi]) * direction,
            "top_rsi": safe(tf_h4["rsi"][ti]),
            "top_adx": safe(tf_h4["adx"][ti]),
            "top_di_diff": safe(tf_h4["dip"][ti] - tf_h4["dim"][ti]) * direction,
            "top_ema_dist": safe((tf_h4["close"][ti] - tf_h4["ema20"][ti]) / tf_h4["atr"][ti]) * direction if tf_h4["atr"][ti] > 0 else 0.0,
            "top_close_vs_ema50": safe(tf_h4["close"][ti] - tf_h4["ema50"][ti]) * direction,
            "hour_jst": float(entry_ts.hour),
            "dow": float(entry_ts.dayofweek),
            "atr_pct_90d": atr_pct_90d,
        })
    df = pd.DataFrame(rows)
    print(f"Total M15 trigger trades: {len(df):,}")

    FEATS = [c for c in df.columns if c not in ("year", "outcome", "net_pts")]

    # Chronological split
    TRAIN_END = 2020
    train = df[df.year <= TRAIN_END].reset_index(drop=True)
    test  = df[df.year >  TRAIN_END].reset_index(drop=True)
    print(f"Train (≤{TRAIN_END}): N={len(train):,}  WR={(train.outcome=='win').mean()*100:.2f}%")
    print(f"Test  (>{TRAIN_END}): N={len(test):,}  WR={(test.outcome=='win').mean()*100:.2f}%")
    print()

    # Fit scaler + cluster on TRAIN WINS only
    Xtr = train[FEATS].to_numpy(dtype=np.float64)
    scaler = StandardScaler().fit(Xtr)
    Xtr_z = scaler.transform(Xtr)
    train_win_mask = (train.outcome == "win").to_numpy()
    km = KMeans(n_clusters=4, n_init=10, random_state=42).fit(Xtr_z[train_win_mask])
    cents_z = km.cluster_centers_

    def assign(X_raw: np.ndarray) -> np.ndarray:
        Xz = scaler.transform(X_raw)
        d2 = ((Xz[:, None, :] - cents_z[None, :, :]) ** 2).sum(axis=2)
        return d2.argmin(axis=1)

    train_assign = assign(Xtr)
    test_assign  = assign(test[FEATS].to_numpy(dtype=np.float64))

    def wilson(wins, n, z=1.96):
        if n == 0: return 0.0, 0.0
        p = wins/n
        den = 1 + z*z/n
        cen = (p + z*z/(2*n)) / den
        rad = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / den
        return max(0, cen-rad), min(1, cen+rad)

    train_base_wr = (train.outcome == "win").mean()
    test_base_wr  = (test.outcome  == "win").mean()

    print("Walk-forward results (cluster centroids fit on TRAIN, applied to TEST):")
    print()
    hdr = f"{'#':<3s} {'N_tr':>6s} {'WR_tr':>7s} | {'N_te':>6s} {'WR_te':>7s} {'drift':>8s}  {'z vs base':>11s}  {'p_raw':>9s} {'BH/Bonf':>9s}"
    print(hdr)
    print("-" * len(hdr))

    from scipy import stats as sp_stats
    results = []
    bonf_alpha = 0.05 / 4
    for ci in range(4):
        m_tr = train_assign == ci
        m_te = test_assign  == ci
        n_tr = int(m_tr.sum()); w_tr = int((train.outcome[m_tr] == "win").sum())
        n_te = int(m_te.sum()); w_te = int((test.outcome[m_te]  == "win").sum())
        wr_tr = w_tr / n_tr if n_tr else 0.0
        wr_te = w_te / n_te if n_te else 0.0
        drift = (wr_te - wr_tr) * 100
        # 2-prop z-test: cluster WR_te vs test baseline
        if n_te > 0:
            p_pool = (w_te + (test.outcome == "win").sum() - w_te) / (n_te + len(test) - n_te)
            # Simpler: chi2 contingency
            other_wins = (test.outcome == "win").sum() - w_te
            other_n = len(test) - n_te
            if other_n > 0:
                p_hat_pool = (w_te + other_wins) / (n_te + other_n)
                se = math.sqrt(p_hat_pool * (1 - p_hat_pool) * (1/n_te + 1/other_n)) if p_hat_pool > 0 and p_hat_pool < 1 else 0
                z_stat = (wr_te - (other_wins/other_n)) / se if se > 0 else 0
                p_raw = 2 * (1 - sp_stats.norm.cdf(abs(z_stat)))
            else:
                z_stat = 0; p_raw = 1.0
        else:
            z_stat = 0; p_raw = 1.0
        sig = "YES" if p_raw < bonf_alpha else "no"
        results.append({
            "ci": ci, "n_tr": n_tr, "wr_tr": wr_tr, "n_te": n_te, "wr_te": wr_te,
            "drift": drift, "z": z_stat, "p_raw": p_raw, "sig": sig,
        })
        print(f"{ci+1:<3d} {n_tr:>6,d} {wr_tr*100:>6.2f}% | "
              f"{n_te:>6,d} {wr_te*100:>6.2f}% {drift:>+7.2f}pp  "
              f"{z_stat:>+11.2f}  {p_raw:>9.2e}  {sig:>9s}")
    print()
    print(f"Test baseline WR: {test_base_wr*100:.2f}%  (N={len(test):,})")
    print(f"Bonferroni-corrected α (k=4): {bonf_alpha:.4f}")
    print()
    # Identify top
    top = max(results, key=lambda r: r["wr_te"])
    top_ci = top["ci"]
    top_lo, top_hi = wilson(int(top["wr_te"]*top["n_te"]+0.5), top["n_te"])
    print(f">>> Highest test-period WR: Cluster #{top_ci+1}")
    print(f"    train WR = {top['wr_tr']*100:.2f}%  →  test WR = {top['wr_te']*100:.2f}%  "
          f"(drift {top['drift']:+.2f}pp)")
    print(f"    test N = {top['n_te']:,}   95%CI = [{top_lo*100:.1f}%, {top_hi*100:.1f}%]")
    print(f"    z = {top['z']:+.2f}   p_raw = {top['p_raw']:.2e}   "
          f"Bonferroni significant? {top['sig']}")
    print()
    if abs(top["drift"]) < 5 and top["sig"] == "YES":
        print("VERDICT: GENUINE — drift < 5pp AND significant after Bonferroni.")
    elif abs(top["drift"]) < 10 and top["sig"] == "YES":
        print("VERDICT: WEAK SIGNAL — moderate drift but still significant.")
    elif abs(top["drift"]) >= 10:
        print("VERDICT: OVERFIT / SELECTION BIAS — drift ≥10pp, pattern doesn't hold out-of-sample.")
    else:
        print("VERDICT: INCONCLUSIVE — fails Bonferroni threshold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
