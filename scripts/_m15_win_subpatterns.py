"""M15 trigger-only win-subpattern extraction.

Pulls 12,194 strict-SPEC M15 DWS-SMT trigger trades (16y XAUUSD CSV),
clusters the WIN subset into k=4 on a 21-feature vector, then reassigns
every trigger trade to its nearest centroid and reports the empirical
win-rate per cluster. Output: the cluster whose feature fingerprint is
"most win-like" and the rate at which trades matching it actually win.
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


def build_tf(label: str, df: pd.DataFrame) -> dict:
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


def at_or_before(arr_ns: np.ndarray, target: int) -> int | None:
    pos = int(np.searchsorted(arr_ns, target, side="right") - 1)
    return pos if pos >= 0 else None


def last_closed(arr_ns: np.ndarray, target: int) -> int | None:
    idx = at_or_before(arr_ns, target)
    if idx is None:
        return None
    return idx - 1 if idx >= 1 else None


def safe(v) -> float:
    if v is None:
        return 0.0
    fv = float(v)
    if math.isnan(fv) or math.isinf(fv):
        return 0.0
    return fv


def main() -> int:
    SLOPE_LOOKBACK = 5

    frames = {tf: _load_tf(tf, XAUUSD_POINT) for tf in ("W1", "D1", "H4", "H1", "M15")}
    result = dws_smt.compute_symbol(
        frames=frames, stacks=config.DWS_SMT_STACKS,
        period=config.DWS_SMT_PERIOD, smooth=config.DWS_SMT_SMOOTH,
        out_bars=max(len(df) for df in frames.values()) + 100,
    )
    w = result.by_base["M15"]
    tf_h4  = build_tf("H4",  frames["H4"])
    tf_h1  = build_tf("H1",  frames["H1"])
    tf_m15 = build_tf("M15", frames["M15"])
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
            "outcome": "win" if net > 0 else "loss",
            "net_pts": net, "direction": direction,
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
    n_total = len(df)
    n_win = int((df.outcome == "win").sum())
    n_loss = int((df.outcome == "loss").sum())
    baseline_wr = n_win / n_total
    print(f"M15 trigger trades: {n_total:,}  wins={n_win:,}  losses={n_loss:,}")
    print(f"Baseline WR: {baseline_wr*100:.2f}%")
    print()

    FEATS = [c for c in df.columns if c not in ("outcome", "net_pts", "direction")]
    X = df[FEATS].to_numpy(dtype=np.float64)
    scaler = StandardScaler().fit(X)
    Xz = scaler.transform(X)

    # Cluster WINS only — find the feature fingerprints of winning trades
    mask_w = (df.outcome == "win").to_numpy()
    km = KMeans(n_clusters=4, n_init=10, random_state=42).fit(Xz[mask_w])

    # Reassign all trades to nearest of the 4 win-shape centroids
    d2 = ((Xz[:, None, :] - km.cluster_centers_[None, :, :]) ** 2).sum(axis=2)
    assign = d2.argmin(axis=1)

    def wilson(wins: int, n: int, z: float = 1.96):
        if n == 0:
            return 0.0, 0.0
        p = wins / n
        den = 1 + z * z / n
        cen = (p + z * z / (2 * n)) / den
        rad = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
        return max(0.0, cen - rad), min(1.0, cen + rad)

    print("Cluster WINS only (k=4) -> reassign every trigger trade to nearest centroid:")
    print(f"{'#':<3s} {'N':>7s} {'wins':>7s} {'WR':>7s}  {'CI95':>16s}  {'vs base':>9s}  {'median':>9s}")
    print("-" * 84)
    results = []
    for ci in range(4):
        m = assign == ci
        n = int(m.sum())
        w_ = int((df.outcome[m] == "win").sum())
        wr = w_ / n if n else 0.0
        lo, hi = wilson(w_, n)
        med = float(df.net_pts[m].median()) if n else 0.0
        delta = (wr - baseline_wr) * 100
        results.append({
            "cluster": ci, "n": n, "wins": w_, "wr": wr,
            "ci_lo": lo, "ci_hi": hi, "median": med, "delta": delta,
            "centroid_z": km.cluster_centers_[ci].copy(),
        })
        print(f"{ci+1:<3d} {n:>7,d} {w_:>7,d} {wr*100:>6.2f}%  "
              f"{lo*100:>5.1f} - {hi*100:>4.1f}%  {delta:>+7.2f}pt  {med:>+8.0f}")
    print()
    results.sort(key=lambda r: -r["wr"])
    top = results[0]
    print(f">>> Highest-WR sub-pattern: Cluster #{top['cluster']+1}")
    print(f"    WR     = {top['wr']*100:.2f}%   (baseline {baseline_wr*100:.2f}%, +{top['delta']:.2f}pt)")
    print(f"    N      = {top['n']:,}   wins = {top['wins']:,}")
    print(f"    CI 95% = [{top['ci_lo']*100:.1f}%, {top['ci_hi']*100:.1f}%]")
    print(f"    median = {top['median']:+.0f} pt")
    print()
    top_raw = scaler.inverse_transform(top["centroid_z"].reshape(1, -1))[0]
    print(f"Feature fingerprint of cluster #{top['cluster']+1} (raw values, direction-signed):")
    for f, v in zip(FEATS, top_raw):
        print(f"   {f:24s} = {v:+.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
