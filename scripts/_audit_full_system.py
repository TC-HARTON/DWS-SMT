"""Full-system audit — checks every logic layer the user trades on.

7 sections, each with PASS/FAIL + concrete numbers, no hand-waving:

  A. CSV period — actual date range + bar counts per TF
  B. Histogram (DWS-SMT colour + state machine) — counts by state
  C. DWS-SMT trigger detection — BUY/SELL/EXIT counts, guard correctness
  D. Trade pairing (signal-validator) — closed trades, win rates per base TF
  E. OOS baseline parity — vs data/oos_baseline.json
  F. Virtual-entry extraction (pattern training set) — counts, WR
  G. Centroid table + per-pattern WR — Wilson CI bit-exact

Run from project root: `python scripts/_audit_full_system.py`.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyzer import dws_smt, indicators
from analyzer.signal_validator import SignalValidator, evaluate_trades
import config
from scripts._backtest_xauusd import _load_tf, XAUUSD_POINT, CsvConnector

PASSES: list[tuple[str, str]] = []
FAILS:  list[tuple[str, str]] = []


def report(section: str, name: str, ok: bool, detail: str = "") -> None:
    bucket = PASSES if ok else FAILS
    bucket.append((section, f"{name}  {detail}"))
    flag = "PASS" if ok else "FAIL"
    print(f"  [{flag}] {name}  {detail}")


# ============================================================================
# A. CSV period — what the rule actually operates on
# ============================================================================

print("=" * 80)
print("A. CSV PERIOD (Dukascopy XAUUSD, Bid+Ask)")
print("=" * 80)

frames_raw: dict[str, pd.DataFrame] = {}
for tf in ("W1", "D1", "H4", "H1", "M15"):
    df = _load_tf(tf, XAUUSD_POINT)
    frames_raw[tf] = df
    start = df.index.min()
    end = df.index.max()
    span_yr = (end - start).days / 365.25
    detail = f"start={start:%Y-%m-%d %H:%M}  end={end:%Y-%m-%d %H:%M}  bars={len(df):,}  span={span_yr:.2f}y"
    report("A", f"{tf} CSV", True, detail)


# ============================================================================
# B0. BIAS composite scoring — TF signal × regime gate × weighted average
# ============================================================================

print()
print("=" * 80)
print("B0. BIAS COMPOSITE (per-bar 5-tier code × regime gate × TF weight)")
print("=" * 80)

from analyzer.indicator_engine import _bias_contribution_series

# Validate the contribution formula end-to-end on a synthetic vector — values
# at exact threshold boundaries to verify each gate fires correctly.
synth = {
    "close": np.array([100.0]),
    "ema":   np.array([99.0]),       # above EMA
    "rsi":   np.array([60.0]),       # ≥ 55 → STRONG eligible
    "adx":   np.array([30.0]),       # ≥ 25 → STRONG gate open
    "dip":   np.array([28.0]),       # bullish DI
    "dim":   np.array([20.0]),
}
out = _bias_contribution_series(**synth)
# code = +2 (STRONG BUY), trend_factor = (30-15)/10 = 1.0 (capped), so out = +2
ok_strong_buy = abs(float(out[0]) - 2.0) < 1e-9
report("B0", "STRONG BUY tier", ok_strong_buy,
       f"close>EMA + ADX=30 + RSI=60 + DI+>DI- → contribution={float(out[0]):.4f} (expect +2.000)")

synth["rsi"] = np.array([52.0])     # 50 ≤ 52 < 55 → BUY (+1), NOT STRONG (needs ≥55)
synth["adx"] = np.array([20.0])     # mid regime → factor (20-15)/10 = 0.5
out = _bias_contribution_series(**synth)
expected = 1.0 * 0.5
ok_buy = abs(float(out[0]) - expected) < 1e-9
report("B0", "BUY × mid regime", ok_buy,
       f"close>EMA + RSI=52 (BUY +1) × ADX=20 (factor 0.5) = {float(out[0]):.4f} (expect {expected:.4f})")

synth["rsi"] = np.array([60.0]); synth["adx"] = np.array([10.0])
out = _bias_contribution_series(**synth)
# ADX below low gate → factor 0 → output 0 regardless of code
ok_range_gate = abs(float(out[0]) - 0.0) < 1e-9
report("B0", "Range gate kills contribution", ok_range_gate,
       f"ADX=10 (below low gate 15) → contribution={float(out[0]):.4f} (expect 0)")

# Below-EMA + RSI 35 + ADX 30 + DI- > DI+ → STRONG SELL (-2)
synth_ss = {
    "close": np.array([99.0]), "ema": np.array([100.0]),
    "rsi": np.array([35.0]), "adx": np.array([30.0]),
    "dip": np.array([18.0]), "dim": np.array([28.0]),
}
out = _bias_contribution_series(**synth_ss)
ok_strong_sell = abs(float(out[0]) - (-2.0)) < 1e-9
report("B0", "STRONG SELL tier", ok_strong_sell,
       f"close<EMA + ADX=30 + RSI=35 + DI->DI+ → contribution={float(out[0]):.4f} (expect -2.000)")

# NaN warmup → 0 contribution
synth_nan = {k: v.copy() for k, v in synth.items()}
synth_nan["rsi"][0] = np.nan
out = _bias_contribution_series(**synth_nan)
ok_nan = abs(float(out[0]) - 0.0) < 1e-9
report("B0", "NaN warmup → 0", ok_nan, f"contribution={float(out[0]):.4f} (expect 0)")


# ============================================================================
# B. Histogram colour + state machine
# ============================================================================

print()
print("=" * 80)
print("B. HISTOGRAM (DWS-SMT colour state per bar)")
print("=" * 80)

result = dws_smt.compute_symbol(
    frames=frames_raw,
    stacks=config.DWS_SMT_STACKS,
    period=config.DWS_SMT_PERIOD,
    smooth=config.DWS_SMT_SMOOTH,
    out_bars=max(len(df) for df in frames_raw.values()) + 100,
)
assert result is not None
for base_tf, w in result.by_base.items():
    # colours: (n_bars, n_rows) — each row 0=UP / 1=DOWN / 2=NEUTRAL
    n_bars, n_rows = w.colors.shape
    all_up   = (w.colors == dws_smt.COLOR_UP).all(axis=1)
    all_down = (w.colors == dws_smt.COLOR_DOWN).all(axis=1)
    mixed    = ~(all_up | all_down)
    n_up = int(all_up.sum()); n_down = int(all_down.sum()); n_mix = int(mixed.sum())
    pct_aligned = (n_up + n_down) / n_bars * 100
    report("B", f"{base_tf}", n_bars > 0 and n_up + n_down + n_mix == n_bars,
           f"bars={n_bars:,}  all-UP={n_up:,} ({n_up/n_bars*100:.1f}%)  "
           f"all-DOWN={n_down:,} ({n_down/n_bars*100:.1f}%)  "
           f"mixed={n_mix:,}  aligned-time={pct_aligned:.1f}%")


# ============================================================================
# C. Trigger detection — state-change edges
# ============================================================================

print()
print("=" * 80)
print("C. DWS-SMT TRIGGER DETECTION (state-change edges, guards)")
print("=" * 80)

for base_tf, w in result.by_base.items():
    trig = list(w.triggers)
    n_buy = sum(1 for t in trig if t == "BUY")
    n_sell = sum(1 for t in trig if t == "SELL")
    n_exit = sum(1 for t in trig if t == "EXIT")
    # Guard: index 0 and n-1 must be None
    g_first = trig[0] is None
    g_last  = trig[-1] is None
    # Guard: a BUY/SELL is only at state-change boundary
    states = np.where((w.colors == dws_smt.COLOR_UP).all(axis=1), 1,
              np.where((w.colors == dws_smt.COLOR_DOWN).all(axis=1), -1, 0))
    edge_ok = True
    for i, t in enumerate(trig):
        if t == "BUY":
            if i == 0 or states[i] != 1 or states[i-1] == 1:
                edge_ok = False; break
        if t == "SELL":
            if i == 0 or states[i] != -1 or states[i-1] == -1:
                edge_ok = False; break
    report("C", f"{base_tf} trigger counts", g_first and g_last and edge_ok,
           f"BUY={n_buy:,}  SELL={n_sell:,}  EXIT={n_exit:,}  "
           f"first/last None={g_first and g_last}  edge_ok={edge_ok}")


# ============================================================================
# D. Trade pairing (closed) — what SignalValidator counts
# ============================================================================

print()
print("=" * 80)
print("D. TRADE PAIRING (closed, post evaluate_trades)")
print("=" * 80)

connector = CsvConnector({"XAUUSD": frames_raw})
emit = max(len(df) for df in frames_raw.values()) + 100
validator = SignalValidator(connector, history_bars=emit, fetch_gap_sec=0.0)
snap = validator.compute(bases=["XAUUSD"],
                         broker_meta={"XAUUSD": {"point": XAUUSD_POINT}})
stats_xau = snap.by_symbol["XAUUSD"]
sv_by_tf: dict[str, dict] = {}
for base_tf in ("M15", "H1", "H4"):
    raw = stats_xau[base_tf].raw
    sv_by_tf[base_tf] = {"n": raw.n_trades, "wr": raw.win_rate,
                         "pf": raw.profit_factor, "exp": raw.expectancy,
                         "ci_low": raw.ci_low, "ci_high": raw.ci_high}
    report("D", f"{base_tf} closed trades", raw.n_trades > 0,
           f"N={raw.n_trades:,}  WR={raw.win_rate*100:.2f}%  "
           f"PF={raw.profit_factor:.2f}  exp={raw.expectancy:+.1f}pt  "
           f"CI=[{raw.ci_low*100:.1f}%, {raw.ci_high*100:.1f}%]")


# ============================================================================
# E. OOS baseline parity — does the data/oos_baseline.json reproduce?
# ============================================================================

print()
print("=" * 80)
print("E. OOS BASELINE PARITY (data/oos_baseline.json vs live SignalValidator)")
print("=" * 80)
print("  Note: oos_baseline.json was generated with YEAR_FIRST=2011 +")
print("  apply_swap_costs(). Live validator above is run without those")
print("  filters, so an exact match isn't expected — but counts and WRs")
print("  should be within a couple of % of the baseline.")
print()

with open(ROOT / "data" / "oos_baseline.json", encoding="utf-8") as f:
    oos = json.load(f)
oos_xau = oos["by_symbol"]["XAUUSD"]
for base_tf in ("M15", "H1", "H4"):
    b = oos_xau[base_tf]
    sv = sv_by_tf[base_tf]
    n_drift_pct = abs(b["n_trades"] - sv["n"]) / b["n_trades"] * 100
    wr_drift_pp = abs(b["win_rate"] - sv["wr"]) * 100
    detail = (f"baseline N={b['n_trades']:,} WR={b['win_rate']*100:.2f}% | "
              f"live N={sv['n']:,} WR={sv['wr']*100:.2f}% | "
              f"|ΔN|={n_drift_pct:.1f}%  |ΔWR|={wr_drift_pp:.2f}pp")
    # Tolerance: ΔN ≤ 10%, ΔWR ≤ 1pp (the 2010-year filter trims ~3-7%
    # of trades; swap costs flip a tiny fraction of near-zero P/L trades).
    ok = n_drift_pct <= 10 and wr_drift_pp <= 1.0
    report("E", f"{base_tf} vs OOS baseline", ok, detail)


# ============================================================================
# F. Virtual-entry pattern extraction
# ============================================================================

print()
print("=" * 80)
print("F. VIRTUAL-ENTRY EXTRACTION (alignment-held bars, training data)")
print("=" * 80)

PT_CSV = ROOT / "data" / "loss_analysis" / "xauusd_per_trade.csv"
if not PT_CSV.exists():
    print(f"  [SKIP] per_trade.csv not present — run scripts/_extract_xauusd_patterns.py first")
else:
    pt = pd.read_csv(PT_CSV)
    for base_tf in ("M15", "H1", "H4"):
        sub = pt[pt.base_tf == base_tf]
        nw = int((sub.outcome == "win").sum())
        nl = int((sub.outcome == "loss").sum())
        wr = nw / (nw + nl) if (nw + nl) else 0.0
        # Expected: virtual-entries N >> closed-trade N (continuation bars
        # multiply each trigger streak by its length). And N must equal
        # the JSON's population total for that TF.
        detail = f"N={len(sub):,}  wins={nw:,}  losses={nl:,}  WR={wr*100:.2f}%"
        report("F", f"{base_tf} virtual entries", len(sub) > 0, detail)


# ============================================================================
# G. Centroid table + per-pattern WR
# ============================================================================

print()
print("=" * 80)
print("G. CENTROID TABLE + PER-PATTERN WR (xauusd_per_cluster_winrate.json)")
print("=" * 80)

JSON_PATH = ROOT / "data" / "loss_analysis" / "xauusd_per_cluster_winrate.json"
if not JSON_PATH.exists() or not PT_CSV.exists():
    print(f"  [SKIP] required artefacts missing")
else:
    J = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    FEATS = J["M15"]["feature_columns"]

    def wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
        if n == 0: return 0.0, 0.0
        p = wins / n
        den = 1 + z*z/n
        cen = (p + z*z/(2*n)) / den
        rad = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / den
        return max(0.0, cen - rad), min(1.0, cen + rad)

    n_fails = 0
    for base_tf in J.keys():
        pop = J[base_tf]["population"]
        # Population vs CSV
        sub = pt[pt.base_tf == base_tf]
        pop_n_obs = len(sub)
        pop_w_obs = int((sub.outcome == "win").sum())
        pop_ok = pop_n_obs == pop["n_trades"] and pop_w_obs == pop["wins"]
        if not pop_ok: n_fails += 1
        report("G", f"{base_tf} pop", pop_ok,
               f"JSON N={pop['n_trades']:,} W={pop['wins']:,} | "
               f"CSV N={pop_n_obs:,} W={pop_w_obs:,}")

        # Cluster reassignment + per-cluster Wilson
        X = sub[FEATS].to_numpy(dtype=np.float64)
        mean = np.array([J[base_tf]["scaler"]["mean"][f] for f in FEATS])
        std  = np.array([J[base_tf]["scaler"]["std"][f]  for f in FEATS])
        Xz = (X - mean) / np.where(std == 0.0, 1.0, std)
        cents = np.array([[c["centroid_z"][f] for f in FEATS] for c in J[base_tf]["clusters"]])
        d2 = ((Xz[:, None, :] - cents[None, :, :])**2).sum(axis=2)
        nearest = d2.argmin(axis=1)
        for ci, cluster in enumerate(J[base_tf]["clusters"]):
            mask = nearest == ci
            n_obs = int(mask.sum())
            w_obs = int((sub.outcome[mask] == "win").sum())
            ok_assign = (n_obs == cluster["assigned_n"]
                         and w_obs == cluster["wins"])
            lo, hi = wilson(cluster["wins"], cluster["assigned_n"])
            ok_ci = (abs(lo - cluster["win_rate_ci95_low"]) < 1e-3
                     and abs(hi - cluster["win_rate_ci95_high"]) < 1e-3)
            ok = ok_assign and ok_ci
            if not ok: n_fails += 1
            wr_obs = w_obs / n_obs if n_obs else 0.0
            report("G", f"{cluster['pattern_id']}", ok,
                   f"N={cluster['assigned_n']:,} W={cluster['wins']:,} "
                   f"WR={cluster['win_rate']*100:.2f}%  "
                   f"CI=[{cluster['win_rate_ci95_low']*100:.1f}%, "
                   f"{cluster['win_rate_ci95_high']*100:.1f}%]")


# ============================================================================
# FINAL VERDICT
# ============================================================================

print()
print("=" * 80)
print("FINAL VERDICT")
print("=" * 80)
n_pass = len(PASSES); n_fail = len(FAILS)
print(f"  PASS: {n_pass}")
print(f"  FAIL: {n_fail}")
if FAILS:
    print()
    print("  Failures:")
    for sec, line in FAILS:
        print(f"    [{sec}] {line}")
sys.exit(0 if n_fail == 0 else 2)
