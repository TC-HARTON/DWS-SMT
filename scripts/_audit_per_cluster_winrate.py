"""Strict numeric audit of xauusd_per_cluster_winrate.json vs xauusd_per_trade.csv.

Runs 5 numeric checks + 1 replay test + edge-case probes.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler
from scipy.optimize import linear_sum_assignment

ROOT = Path(__file__).resolve().parent.parent
JSON_PATH = ROOT / "data" / "loss_analysis" / "xauusd_per_cluster_winrate.json"
CSV_PATH  = ROOT / "data" / "loss_analysis" / "xauusd_per_trade.csv"

TABLE = json.loads(JSON_PATH.read_text(encoding="utf-8"))
LEDGER = pd.read_csv(CSV_PATH)

FEATURE_COLS = tuple(TABLE["M15"]["feature_columns"])

print("="*80)
print(f"Audit: {JSON_PATH.name} vs {CSV_PATH.name}")
print(f"Rows in CSV: {len(LEDGER):,d}")
print(f"Base TFs in JSON: {list(TABLE.keys())}")
print(f"Features: {len(FEATURE_COLS)}")
print("="*80)


# ---------- helper: Wilson 95% CI ----------
def wilson_ci(wins: int, n: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1.0 + z*z/n
    center = (p + z*z/(2*n)) / denom
    half = (z * math.sqrt(p*(1-p)/n + z*z/(4*n*n))) / denom
    return (center - half, center + half)


# ============================================================================
# CHECK 1: per-TF scaler matches StandardScaler.fit(per_trade)
# ============================================================================
print("\n[CHECK 1] Per-TF StandardScaler reproducibility (tol 1e-4)")
print("-" * 60)
check1_pass = True
check1_max = 0.0
check1_worst = None
for tf in TABLE.keys():
    sub = LEDGER[LEDGER.base_tf == tf]
    X = sub[list(FEATURE_COLS)].to_numpy(dtype=np.float64)
    sc = StandardScaler().fit(X)
    js_mean = np.array([TABLE[tf]["scaler"]["mean"][c] for c in FEATURE_COLS])
    js_std  = np.array([TABLE[tf]["scaler"]["std"][c]  for c in FEATURE_COLS])
    d_mean = np.abs(sc.mean_ - js_mean)
    d_std  = np.abs(sc.scale_ - js_std)
    worst = max(d_mean.max(), d_std.max())
    if worst > check1_max:
        check1_max = worst
        idx = int(np.argmax(np.maximum(d_mean, d_std)))
        check1_worst = (tf, FEATURE_COLS[idx], float(d_mean[idx]), float(d_std[idx]),
                        float(sc.mean_[idx]), float(js_mean[idx]),
                        float(sc.scale_[idx]), float(js_std[idx]))
    if worst > 1e-4:
        check1_pass = False
    print(f"  {tf}: n={len(sub):,d}  max|Δmean|={d_mean.max():.2e}  max|Δstd|={d_std.max():.2e}")
print(f"  >> max delta across all TFs: {check1_max:.2e}")
if check1_worst:
    tf, c, dm, ds, m_obs, m_js, s_obs, s_js = check1_worst
    print(f"  >> worst @ {tf}/{c}: mean obs={m_obs:.6f} js={m_js:.6f}  std obs={s_obs:.6f} js={s_js:.6f}")
print(f"  RESULT: {'PASS' if check1_pass else 'FAIL'}")


# ============================================================================
# CHECK 2: centroid_z * std + mean ↔ centroid_raw round-trip
# ============================================================================
print("\n[CHECK 2] centroid_z * std + mean ↔ centroid_raw (tol 1e-3, all 24 clusters)")
print("-" * 60)
check2_pass = True
check2_max = 0.0
check2_worst = None
total_clusters = 0
for tf in TABLE.keys():
    js_mean = np.array([TABLE[tf]["scaler"]["mean"][c] for c in FEATURE_COLS])
    js_std  = np.array([TABLE[tf]["scaler"]["std"][c]  for c in FEATURE_COLS])
    for cl in TABLE[tf]["clusters"]:
        total_clusters += 1
        cz  = np.array([cl["centroid_z"][c]   for c in FEATURE_COLS])
        cr  = np.array([cl["centroid_raw"][c] for c in FEATURE_COLS])
        reconstructed = cz * js_std + js_mean
        d = np.abs(reconstructed - cr)
        if d.max() > check2_max:
            check2_max = d.max()
            idx = int(np.argmax(d))
            check2_worst = (tf, cl["pattern_id"], FEATURE_COLS[idx],
                            float(reconstructed[idx]), float(cr[idx]), float(d[idx]))
        if d.max() > 1e-3:
            check2_pass = False
print(f"  >> {total_clusters} clusters checked")
print(f"  >> max delta: {check2_max:.2e}")
if check2_worst:
    tf, pid, c, recon, raw, delta = check2_worst
    print(f"  >> worst @ {tf}/{pid}/{c}: z*std+mean={recon:.6f}  raw={raw:.6f}  Δ={delta:.2e}")
print(f"  RESULT: {'PASS' if check2_pass else 'FAIL'}")


# ============================================================================
# CHECK 3: cluster assignment via nearest-centroid -> assigned_n, wins, win_rate
# ============================================================================
print("\n[CHECK 3] Nearest-centroid reassignment -> assigned_n / wins / win_rate")
print("-" * 60)
check3_pass = True
check3_problems = []
# Store assignments per TF for replay test reuse
all_assignments: dict[str, np.ndarray] = {}
for tf in TABLE.keys():
    sub = LEDGER[LEDGER.base_tf == tf].reset_index(drop=True)
    X = sub[list(FEATURE_COLS)].to_numpy(dtype=np.float64)
    js_mean = np.array([TABLE[tf]["scaler"]["mean"][c] for c in FEATURE_COLS])
    js_std  = np.array([TABLE[tf]["scaler"]["std"][c]  for c in FEATURE_COLS])
    js_std_safe = np.where(js_std == 0.0, 1.0, js_std)
    Xz = (X - js_mean) / js_std_safe

    centz = np.array(
        [[cl["centroid_z"][c] for c in FEATURE_COLS] for cl in TABLE[tf]["clusters"]],
        dtype=np.float64,
    )
    # Pairwise euclidean
    diffs = Xz[:, None, :] - centz[None, :, :]
    dists = np.sqrt((diffs ** 2).sum(axis=2))
    assigned = dists.argmin(axis=1)
    all_assignments[tf] = assigned

    for ci, cl in enumerate(TABLE[tf]["clusters"]):
        mask = (assigned == ci)
        obs_n = int(mask.sum())
        obs_wins = int((sub.loc[mask, "outcome"] == "win").sum())
        obs_losses = int((sub.loc[mask, "outcome"] == "loss").sum())
        obs_wr = obs_wins / obs_n if obs_n else 0.0

        js_n = int(cl["assigned_n"])
        js_w = int(cl["wins"])
        js_l = int(cl["losses"])
        js_wr = float(cl["win_rate"])

        if obs_n != js_n or obs_wins != js_w or obs_losses != js_l:
            check3_pass = False
            check3_problems.append(
                f"  FAIL {tf}/{cl['pattern_id']}: obs(n={obs_n} w={obs_wins} l={obs_losses}) "
                f"vs json(n={js_n} w={js_w} l={js_l})"
            )
        if abs(obs_wr - js_wr) > 5e-4:
            check3_pass = False
            check3_problems.append(
                f"  FAIL {tf}/{cl['pattern_id']}: obs_wr={obs_wr:.6f} json_wr={js_wr:.6f}"
            )

if check3_problems:
    for p in check3_problems[:15]:
        print(p)
    if len(check3_problems) > 15:
        print(f"  ... ({len(check3_problems)-15} more)")
else:
    # show per-TF summary
    for tf in TABLE.keys():
        n_tot = int((LEDGER.base_tf == tf).sum())
        print(f"  {tf}: all 8 clusters reconcile  (n={n_tot:,d})")
print(f"  RESULT: {'PASS' if check3_pass else 'FAIL'}")


# ============================================================================
# CHECK 4: Wilson 95% CI recomputed (tol 1e-4 per cluster, all 24)
# ============================================================================
print("\n[CHECK 4] Wilson 95% CI bit-match (tol 1e-4 per bound, 24 clusters)")
print("-" * 60)
check4_pass = True
check4_max = 0.0
check4_worst = None
for tf in TABLE.keys():
    for cl in TABLE[tf]["clusters"]:
        n = int(cl["assigned_n"])
        w = int(cl["wins"])
        lo_calc, hi_calc = wilson_ci(w, n)
        lo_js = float(cl["win_rate_ci95_low"])
        hi_js = float(cl["win_rate_ci95_high"])
        d = max(abs(lo_calc - lo_js), abs(hi_calc - hi_js))
        if d > check4_max:
            check4_max = d
            check4_worst = (tf, cl["pattern_id"], lo_calc, lo_js, hi_calc, hi_js, d)
        if d > 1e-4:
            check4_pass = False
print(f"  >> max |Δ| across all 24 clusters: {check4_max:.2e}")
if check4_worst:
    tf, pid, loC, loJ, hiC, hiJ, d = check4_worst
    print(f"  >> worst @ {tf}/{pid}: lo calc={loC:.6f} js={loJ:.6f}  hi calc={hiC:.6f} js={hiJ:.6f}")
print(f"  RESULT: {'PASS' if check4_pass else 'FAIL'}")


# ============================================================================
# CHECK 5: K-means refit per (tf, outcome) — paired-centroid distance via Hungarian
# ============================================================================
print("\n[CHECK 5] K-means refit (k=4, n_init=10, rs=42), Hungarian-paired centroid Δ (tol 1e-3)")
print("-" * 60)
check5_pass = True
check5_max = 0.0
check5_worst = None
for tf in TABLE.keys():
    sub = LEDGER[LEDGER.base_tf == tf]
    js_mean = np.array([TABLE[tf]["scaler"]["mean"][c] for c in FEATURE_COLS])
    js_std  = np.array([TABLE[tf]["scaler"]["std"][c]  for c in FEATURE_COLS])
    js_std_safe = np.where(js_std == 0.0, 1.0, js_std)

    for shape in ("win", "loss"):
        sub_shape = sub[sub.outcome == shape]
        X = sub_shape[list(FEATURE_COLS)].to_numpy(dtype=np.float64)
        # Use the SHAPE-local scaler if the JSON is per-shape, OR the per-TF scaler.
        # The JSON has only ONE per-TF scaler. So either:
        # (a) the offline script used the per-TF scaler for both shapes' k-means, or
        # (b) it fit a per-shape scaler.
        # We try both and pick whichever matches best.
        # Reference centroids (z) for this shape from JSON:
        ref_z = np.array([
            [cl["centroid_z"][c] for c in FEATURE_COLS]
            for cl in TABLE[tf]["clusters"]
            if cl["learned_from_shape"] == shape
        ], dtype=np.float64)

        if ref_z.shape[0] == 0:
            continue
        k = ref_z.shape[0]

        best_max_d = float("inf")
        best_mode = None
        for mode in ("per_tf", "per_shape"):
            if mode == "per_tf":
                Xz = (X - js_mean) / js_std_safe
            else:
                sc = StandardScaler().fit(X)
                Xz = sc.transform(X)
            km = KMeans(n_clusters=k, n_init=10, random_state=42).fit(Xz)
            cand = km.cluster_centers_
            # Hungarian assignment
            cost = np.zeros((k, k))
            for i in range(k):
                for j in range(k):
                    cost[i, j] = np.linalg.norm(cand[i] - ref_z[j])
            r, c = linear_sum_assignment(cost)
            max_d = float(cost[r, c].max())
            if max_d < best_max_d:
                best_max_d = max_d
                best_mode = mode
        if best_max_d > check5_max:
            check5_max = best_max_d
            check5_worst = (tf, shape, best_mode, best_max_d)
        if best_max_d > 1e-3:
            check5_pass = False
        print(f"  {tf}/{shape:4s}  best_mode={best_mode}  max paired Δ = {best_max_d:.4e}")
print(f"  >> overall max paired Δ: {check5_max:.4e}")
if check5_worst:
    print(f"  >> worst @ {check5_worst[0]}/{check5_worst[1]}  (mode={check5_worst[2]})")
print(f"  RESULT: {'PASS' if check5_pass else 'FAIL'}")


# ============================================================================
# REPLAY TEST — pick 1 trade and replay through scaler + nearest-centroid
# ============================================================================
print("\n[REPLAY] Pick 1 trade, standardize, nearest-centroid, compare to Check 3 assignment")
print("-" * 60)
tf_pick = "M15"
sub_pick = LEDGER[LEDGER.base_tf == tf_pick].reset_index(drop=True)
# Pick a deterministic trade — the very first M15 trade
row_idx = 0
row = sub_pick.iloc[row_idx]
x = np.array([float(row[c]) for c in FEATURE_COLS], dtype=np.float64)
js_mean = np.array([TABLE[tf_pick]["scaler"]["mean"][c] for c in FEATURE_COLS])
js_std  = np.array([TABLE[tf_pick]["scaler"]["std"][c]  for c in FEATURE_COLS])
js_std_safe = np.where(js_std == 0.0, 1.0, js_std)
xz = (x - js_mean) / js_std_safe
centz = np.array(
    [[cl["centroid_z"][c] for c in FEATURE_COLS] for cl in TABLE[tf_pick]["clusters"]],
    dtype=np.float64,
)
dists = np.sqrt(((centz - xz[None, :]) ** 2).sum(axis=1))
ci_replay = int(dists.argmin())
ci_check3 = int(all_assignments[tf_pick][row_idx])
pid_replay = TABLE[tf_pick]["clusters"][ci_replay]["pattern_id"]
pid_check3 = TABLE[tf_pick]["clusters"][ci_check3]["pattern_id"]
replay_pass = (ci_replay == ci_check3)
print(f"  Trade row 0: tf={tf_pick}  entry_ts={row['entry_ts_utc']}  dir={int(row['direction'])}  outcome={row['outcome']}")
print(f"  Distances to 8 centroids: {np.round(dists, 4).tolist()}")
print(f"  argmin = cluster {ci_replay}  ({pid_replay})")
print(f"  Check3 assignment = cluster {ci_check3} ({pid_check3})")
print(f"  RESULT: {'PASS' if replay_pass else 'FAIL'}")


# ============================================================================
# EDGE CASES
# ============================================================================
print("\n[EDGE CASES]")
print("-" * 60)
# NaN / Inf
nan_counts = LEDGER[list(FEATURE_COLS)].isna().sum()
inf_counts = LEDGER[list(FEATURE_COLS)].apply(lambda col: np.isinf(col.to_numpy(dtype=np.float64)).sum())
nan_total = int(nan_counts.sum())
inf_total = int(inf_counts.sum())
print(f"  NaN cells across 21 features: {nan_total}")
print(f"  Inf cells across 21 features: {inf_total}")
if nan_total > 0:
    print(f"    cols with NaN: {nan_counts[nan_counts > 0].to_dict()}")
if inf_total > 0:
    print(f"    cols with Inf: {inf_counts[inf_counts > 0].to_dict()}")

# Duplicates
dupes = LEDGER.duplicated(subset=["entry_ts_utc", "base_tf", "direction"], keep=False)
n_dupes = int(dupes.sum())
print(f"  Duplicate (entry_ts_utc, base_tf, direction): {n_dupes}")
if n_dupes:
    print(LEDGER[dupes].head(5).to_string())

# mid/top vs base magnitudes (rsi/adx)
print("\n  Per-TF feature magnitude sanity (mean across rows):")
for tf in TABLE.keys():
    s = LEDGER[LEDGER.base_tf == tf]
    print(f"   {tf}: base_adx={s.base_adx.mean():.2f}  mid_adx={s.mid_adx.mean():.2f}  top_adx={s.top_adx.mean():.2f}")
    print(f"        base_di_diff={s.base_di_diff.mean():+.2f}  mid_di_diff={s.mid_di_diff.mean():+.2f}  top_di_diff={s.top_di_diff.mean():+.2f}")
    print(f"        base_atr_pct={s.base_atr_pct.mean():.3f}  mid_atr_pct={s.mid_atr_pct.mean():.3f}")

# ============================================================================
# Summary
# ============================================================================
print("\n" + "="*80)
print("FINAL VERDICT")
print("="*80)
results = {
    "Check 1 (scaler)":   check1_pass,
    "Check 2 (z↔raw)":    check2_pass,
    "Check 3 (assign)":   check3_pass,
    "Check 4 (Wilson)":   check4_pass,
    "Check 5 (k-means)":  check5_pass,
    "Replay":             replay_pass,
}
for name, ok in results.items():
    print(f"  {name}: {'PASS' if ok else 'FAIL'}")
all_pass = all(results.values())
print(f"\n  >>> {'numerics sound' if all_pass else 'BUG DETECTED'}")
sys.exit(0 if all_pass else 1)
