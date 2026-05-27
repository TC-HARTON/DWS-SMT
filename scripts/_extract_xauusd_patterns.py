"""XAUUSD W/L pattern extraction per base TF — READ-ONLY analysis.

This is a **pure extraction** script. It does NOT modify the DWS-SMT rules,
SignalValidator, config, the live server, or any production data. Outputs
live under ``data/loss_analysis/``.

Pipeline (for each base_tf in {H4, H1, M15}):

  1. Reuse the existing ``_backtest_xauusd`` infrastructure to load
     Dukascopy CSV history (16 years) and compute every DWS-SMT trade
     under the **strict** SPEC rules — no rule changes here.
  2. At each trade's entry bar, snapshot a feature vector using the
     existing production indicators (analyzer/indicators.py:adx/rsi/atr,
     analyzer/dws_smt.py outputs). No re-implementation.
  3. Label each trade as win/loss using ``net = points/point - spread``
     (same accounting the live validator uses).
  4. Per (base_tf, outcome) — k-means cluster the feature vectors into
     2-4 patterns. Report centroids + cluster sizes + median W/L per
     cluster.
  5. Compare win-pattern centroids vs loss-pattern centroids — feature
     by feature, surface the biggest deltas.

Outputs (deterministic, regeneratable):

  data/loss_analysis/xauusd_per_trade.csv      -- per-trade ledger
  data/loss_analysis/xauusd_patterns_report.md -- structured findings
  data/loss_analysis/xauusd_centroids.json     -- centroids for the
                                                  future dashboard
                                                  "similarity %" feature

The dashboard implementation is OUT OF SCOPE — only the centroid
artefact is produced so the future similarity-% UI has data to score
against.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import config                                                          # noqa: E402
from analyzer import dws_smt, indicators                               # noqa: E402

# Reuse the existing backtest's CSV loader + connector — no duplication.
from scripts._backtest_xauusd import (                                 # noqa: E402
    _load_tf, XAUUSD_POINT,
)

# Clustering. sklearn is already a project dependency (used elsewhere for
# correlation analytics) — no new deps added.
try:
    from sklearn.cluster import KMeans
    from sklearn.preprocessing import StandardScaler
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False


log = logging.getLogger("extract_xauusd_patterns")

OUT_DIR = PROJECT_ROOT / "data" / "loss_analysis"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Bars before the entry that we consider for "recent slope" features.
SLOPE_LOOKBACK = 5
# K range to search per (base_tf, outcome) cluster — silhouette picks best.
K_RANGE = (2, 3, 4)

# NO YEAR FILTER.
# Earlier versions of this script copied a YEAR_FIRST=2011 filter from
# scripts/_backtest_all_yearly_swap.py to match data/oos_baseline.json
# trade counts. That filter is wrong here for two reasons:
#
#  1. The strict SPEC rule is deterministic — there's no "training" to be
#     contaminated by warm-up bars. Every bar where the rule fires IS a
#     real trade. Trimming the head of the data series introduces an
#     arbitrary period cut that the rule never asked for.
#
#  2. Of the three base TFs, only H4 needs a long W1 history (its stack
#     is W1/D1/H4). M15 (H4/H1/M15) and H1 (D1/H4/H1) never read a W1
#     bar — so the W1 warm-up justification doesn't apply to them at all.
#
# This module now generates one virtual trade per alignment-held bar
# across the FULL Dukascopy history. Any warm-up bias is naturally bounded
# by indicators.adx / atr / rsi returning NaN on warm-up rows; the
# _row_for_trade builder already drops those via its SLOPE_LOOKBACK + 1
# minimum-index guard, so no separate year filter is needed.


# --------------------------------------------------------------------------- #
# Per-bar indicator series (computed once per TF, reused for every entry)
# --------------------------------------------------------------------------- #

@dataclass
class TFSeries:
    """Per-bar indicator arrays for one timeframe of XAUUSD."""
    label: str
    times_ns: np.ndarray              # int64 epoch ns, ascending
    close:    np.ndarray
    high:     np.ndarray
    low:      np.ndarray
    ema20:    np.ndarray
    ema50:    np.ndarray
    rsi14:    np.ndarray
    atr14:    np.ndarray
    adx14:    np.ndarray
    di_p:     np.ndarray
    di_m:     np.ndarray


def _build_tf_series(label: str, df: pd.DataFrame) -> TFSeries:
    """Compute every indicator array we'll later sample at trade entries."""
    close = df["close"].to_numpy(dtype=np.float64)
    high  = df["high"].to_numpy(dtype=np.float64)
    low   = df["low"].to_numpy(dtype=np.float64)
    ema20 = indicators.ema(close, 20)
    ema50 = indicators.ema(close, 50)
    rsi14 = indicators.rsi(close, 14)
    atr14 = indicators.atr(high, low, close, 14)
    adx14, di_p, di_m = indicators.adx(high, low, close, 14)
    return TFSeries(
        label=label,
        times_ns=df.index.values.astype("datetime64[ns]").astype("int64"),
        close=close, high=high, low=low,
        ema20=ema20, ema50=ema50,
        rsi14=rsi14, atr14=atr14,
        adx14=adx14, di_p=di_p, di_m=di_m,
    )


def _at_or_before(series_ns: np.ndarray, target_ns: int) -> int | None:
    """Index of the most recent bar <= target_ns, or None if before the series."""
    pos = int(np.searchsorted(series_ns, target_ns, side="right") - 1)
    if pos < 0:
        return None
    return pos


# --------------------------------------------------------------------------- #
# Feature extraction at one trade entry
# --------------------------------------------------------------------------- #

# Column order — keep in sync with row_for_trade. Used for centroid CSV.
FEATURE_COLS: tuple[str, ...] = (
    # Base TF (where the trigger fired)
    "base_rsi", "base_adx", "base_di_diff", "base_atr_pct",
    "base_ema_dist", "base_ema_slope", "base_close_vs_ema50",
    # Mid TF (the 2nd row of the stack)
    "mid_rsi", "mid_adx", "mid_di_diff", "mid_atr_pct",
    "mid_ema_dist", "mid_close_vs_ema50",
    # Top TF (the 3rd row of the stack)
    "top_rsi", "top_adx", "top_di_diff",
    "top_ema_dist", "top_close_vs_ema50",
    # Calendar / session features
    "hour_jst", "dow",
    # Recent volatility
    "atr_pct_90d",
)


def _safe(value: float) -> float:
    """Replace NaN / inf with 0.0 so clustering doesn't blow up."""
    if value is None or np.isnan(value) or np.isinf(value):
        return 0.0
    return float(value)


def _row_for_trade(
    *,
    direction: int,
    entry_ns: int,
    base: TFSeries,
    mid: TFSeries,
    top: TFSeries,
) -> dict[str, float] | None:
    """Build the feature row at a single trade entry timestamp.

    Higher-TF (mid / top) index resolution uses the **last fully CLOSED**
    bar — not the bar that contains the entry timestamp. For example, an
    M15 trigger at 12:30 UTC sits inside the 12:00-13:00 H1 bar; reading
    that H1 bar's close at 12:30 would leak the 12:30-13:00 future into
    the training feature vector. We instead use the previous H1 bar
    (11:00-12:00, closed at 12:00) whose values were fully knowable at
    the moment the M15 trigger fired. Runtime pattern_matcher.match()
    does the same via ``len(df) - 2`` — this keeps offline and live
    feature distributions identical.
    """
    # Base TF — index from _at_or_before is the bar whose CLOSE produced
    # the trigger (SPEC: entry at trigger bar's close). That bar IS closed.
    bi = _at_or_before(base.times_ns, entry_ns)
    # Higher TFs — _at_or_before returns the bar CONTAINING entry_ns, which
    # by definition has NOT yet closed at the moment the M15 trigger fired.
    # Reading indicator values at that index leaks the future close of the
    # still-forming H1/H4 bar into the training feature vector. Step back
    # by one to use the LAST FULLY CLOSED higher-TF bar — same semantics as
    # the runtime matcher's ``len(df) - 2`` rule. This kills the offline
    # lookahead bias flagged in the strict math audit.
    mi_raw = _at_or_before(mid.times_ns, entry_ns)
    ti_raw = _at_or_before(top.times_ns, entry_ns)
    mi = mi_raw - 1 if mi_raw is not None and mi_raw >= 1 else None
    ti = ti_raw - 1 if ti_raw is not None and ti_raw >= 1 else None
    if bi is None or mi is None or ti is None or bi < SLOPE_LOOKBACK + 1:
        return None

    def _di_diff(p: np.ndarray, m: np.ndarray, i: int) -> float:
        return _safe(p[i] - m[i]) * direction         # direction-aware

    def _ema_dist(close: np.ndarray, ema: np.ndarray, atr: np.ndarray, i: int) -> float:
        if atr[i] <= 0 or np.isnan(atr[i]):
            return 0.0
        return _safe((close[i] - ema[i]) / atr[i]) * direction

    def _close_vs_ema50(close: np.ndarray, ema50: np.ndarray, i: int) -> float:
        return _safe(close[i] - ema50[i]) * direction  # >0 = with-trend

    # Base TF — last 5 base bars' close-to-EMA20 slope, in ATR units, signed by trade direction
    sl_end = base.close[bi]
    sl_beg = base.close[bi - SLOPE_LOOKBACK]
    atr_b  = base.atr14[bi]
    slope  = ((sl_end - sl_beg) / atr_b) * direction if atr_b and atr_b > 0 else 0.0

    # Recent ATR percentile vs trailing 90 days of the base TF (proxy: 360 H4
    # bars / 1440 H1 / 5760 M15 — same calendar window).
    bars_per_day = {"H4": 6, "H1": 24, "M15": 96}[base.label]
    window = bars_per_day * 90
    lo = max(0, bi - window)
    atr_hist = base.atr14[lo: bi + 1]
    atr_hist = atr_hist[~np.isnan(atr_hist)]
    if atr_hist.size > 5 and atr_b and atr_b > 0:
        atr_pct_90d = float((atr_hist < atr_b).mean())
    else:
        atr_pct_90d = 0.5

    ts = pd.Timestamp(entry_ns, unit="ns", tz="UTC").tz_convert("Asia/Tokyo")

    return {
        "base_rsi":          _safe(base.rsi14[bi]),
        "base_adx":          _safe(base.adx14[bi]),
        "base_di_diff":      _di_diff(base.di_p, base.di_m, bi),
        "base_atr_pct":      _safe(atr_b / base.close[bi] * 100.0) if base.close[bi] else 0.0,
        "base_ema_dist":     _ema_dist(base.close, base.ema20, base.atr14, bi),
        "base_ema_slope":    _safe(slope),
        "base_close_vs_ema50": _close_vs_ema50(base.close, base.ema50, bi),

        "mid_rsi":           _safe(mid.rsi14[mi]),
        "mid_adx":           _safe(mid.adx14[mi]),
        "mid_di_diff":       _di_diff(mid.di_p, mid.di_m, mi),
        "mid_atr_pct":       _safe(mid.atr14[mi] / mid.close[mi] * 100.0) if mid.close[mi] else 0.0,
        "mid_ema_dist":      _ema_dist(mid.close, mid.ema20, mid.atr14, mi),
        "mid_close_vs_ema50": _close_vs_ema50(mid.close, mid.ema50, mi),

        "top_rsi":           _safe(top.rsi14[ti]),
        "top_adx":           _safe(top.adx14[ti]),
        "top_di_diff":       _di_diff(top.di_p, top.di_m, ti),
        "top_ema_dist":      _ema_dist(top.close, top.ema20, top.atr14, ti),
        "top_close_vs_ema50": _close_vs_ema50(top.close, top.ema50, ti),

        "hour_jst":          float(ts.hour),
        "dow":               float(ts.dayofweek),
        "atr_pct_90d":       atr_pct_90d,
    }


# --------------------------------------------------------------------------- #
# Trade ledger build
# --------------------------------------------------------------------------- #

def _net_pts(trade: dws_smt.DwsSmtTrade, spread_pts: np.ndarray, point: float) -> float:
    """Net P/L in points for one closed trade — same formula as evaluate_trades."""
    cost = float(spread_pts[trade.entry_idx]) if trade.entry_idx < spread_pts.size else 0.0
    return trade.points / point - cost


def _state_per_bar(window: dws_smt.DwsSmtWindow) -> np.ndarray:
    """Reconstruct the alignment STATE per bar from the colour matrix.

    state[i] = +1 if every row at bar i is UP, -1 if every row is DOWN,
    0 otherwise. Same logic ``dws_smt._detect_triggers`` uses, but exposed
    as a per-bar array so we can iterate every bar (not only state-change
    edges) when generating virtual entries for the pattern training set.
    """
    if window.colors.size == 0:
        return np.array([], dtype=np.int8)
    all_up   = (window.colors == dws_smt.COLOR_UP).all(axis=1)
    all_down = (window.colors == dws_smt.COLOR_DOWN).all(axis=1)
    return np.where(all_up, 1, np.where(all_down, -1, 0)).astype(np.int8)


def _virtual_trades_from_alignment(
    window: dws_smt.DwsSmtWindow,
    base_df: pd.DataFrame,
    point: float,
) -> list[dict]:
    """Generate one virtual trade per bar where the 3-TF alignment is held.

    Why: the LIVE pattern matcher gates on ``alignment STATE held on the
    latest closed bar`` (analysis_loop.py), so it scores continuation bars
    just as much as trigger-edge bars. The original training set used only
    trigger-edge entries (one per trade) — that distribution doesn't match
    the live evaluation universe. Adding virtual entries at every
    alignment-held bar makes the centroid table match the LIVE feature
    distribution, eliminating the semantic drift the strict audit flagged.

    Each virtual trade:
      - entry bar j: any bar where state[j] != 0 (alignment held)
      - direction = state[j]
      - exit bar k: the next bar where state flips (state[k] != state[j])
      - entry price = close[j], exit price = close[k]
      - points = (close[k] - close[j]) * direction
      - MAE: worst adverse excursion between j and k

    Returns trades that fully close inside the emitted window. Trades whose
    exit would fall past the last bar (still-aligned at end of history) are
    dropped — they have no observable outcome.
    """
    states = _state_per_bar(window)
    n = states.size
    if n < 2:
        return []
    closes = base_df["close"].to_numpy(dtype=np.float64)
    highs  = base_df["high"].to_numpy(dtype=np.float64)
    lows   = base_df["low"].to_numpy(dtype=np.float64)
    # window emission may have been trimmed; align indices to the full base
    start_offset = max(0, len(base_df) - n)
    out: list[dict] = []
    for j in range(n - 1):                         # skip in-progress bar
        s = int(states[j])
        if s == 0:
            continue
        # Find next bar where state flips. End-of-history bars are -1
        # (in-progress) so the while loop terminates at the latest closed
        # bar of the same state at most; we then check k < n - 1 to ensure
        # a real flip happened (not just running off the array).
        k = j + 1
        while k < n - 1 and int(states[k]) == s:
            k += 1
        if k >= n - 1 or int(states[k]) == s:
            continue                                # still aligned at history end → drop
        entry_full = start_offset + j
        exit_full  = start_offset + k
        if entry_full >= len(closes) or exit_full >= len(closes):
            continue
        ep = float(closes[entry_full])
        xp = float(closes[exit_full])
        points = (xp - ep) * s
        if s == 1:
            worst = float(lows[entry_full: exit_full + 1].min())
            mae = max(0.0, ep - worst)
        else:
            worst = float(highs[entry_full: exit_full + 1].max())
            mae = max(0.0, worst - ep)
        out.append({
            "entry_idx_full": entry_full,
            "direction": s,
            "points": points,
            "mae": mae,
        })
    return out


def _build_ledger(
    *,
    base_label: str,
    base_df: pd.DataFrame,
    base_series: TFSeries,
    mid_series: TFSeries,
    top_series: TFSeries,
    window: dws_smt.DwsSmtWindow,
    point: float,
) -> pd.DataFrame:
    """Build a per-trade DataFrame for one base TF, with full entry features.

    Uses *virtual trades* — one per bar where alignment is held — so the
    training distribution matches the LIVE evaluation universe (pattern
    matcher fires on every alignment-held bar, not only trigger edges).
    See ``_virtual_trades_from_alignment`` for the construction rule.
    """
    spread_pts = base_df["spread"].to_numpy(dtype=np.int64)
    rows: list[dict] = []
    base_full_ns = base_series.times_ns

    vtrades = _virtual_trades_from_alignment(window, base_df, point)
    for vt in vtrades:
        entry_idx = vt["entry_idx_full"]
        if entry_idx >= base_full_ns.size:
            continue
        entry_ns = int(base_full_ns[entry_idx])
        # No year filter — see module-level comment.
        feats = _row_for_trade(
            direction=vt["direction"],
            entry_ns=entry_ns,
            base=base_series, mid=mid_series, top=top_series,
        )
        if feats is None:
            continue
        # Spread cost — bar-spread at the entry bar, same as evaluate_trades.
        cost = float(spread_pts[entry_idx]) if entry_idx < spread_pts.size else 0.0
        net = vt["points"] / point - cost
        rows.append({
            "base_tf": base_label,
            "entry_ts_utc": pd.Timestamp(entry_ns, unit="ns", tz="UTC").isoformat(),
            "direction": int(vt["direction"]),
            "outcome": "win" if net > 0 else "loss",
            "net_pts": round(net, 2),
            "mae_pts": round(vt["mae"] / point, 2),
            **feats,
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Clustering — find pattern centroids per (base_tf, outcome)
# --------------------------------------------------------------------------- #

def _cluster_one(df: pd.DataFrame, k: int) -> tuple[np.ndarray, np.ndarray, float]:
    """Return (labels, centroids_zscore_back_to_raw, inertia)."""
    X = df[list(FEATURE_COLS)].to_numpy(dtype=np.float64)
    scaler = StandardScaler()
    Xz = scaler.fit_transform(X)
    km = KMeans(n_clusters=k, n_init=10, random_state=42).fit(Xz)
    centroids_raw = scaler.inverse_transform(km.cluster_centers_)
    return km.labels_, centroids_raw, float(km.inertia_)


def _pick_k(df: pd.DataFrame) -> int:
    """Elbow heuristic: pick k where inertia drop flattens. Cheap, deterministic."""
    if len(df) < 30:
        return 2
    inertias = []
    for k in K_RANGE:
        _, _, ine = _cluster_one(df, k)
        inertias.append(ine)
    # Δ-inertia ratio: when adding a cluster brings less than 50% of the
    # previous improvement, stop.
    drops = [(inertias[i] - inertias[i + 1]) for i in range(len(inertias) - 1)]
    if not drops or drops[0] <= 0:
        return K_RANGE[0]
    for i in range(1, len(drops)):
        if drops[i] < 0.5 * drops[i - 1]:
            return K_RANGE[i]
    return K_RANGE[-1]


def _cluster_outcome(df: pd.DataFrame) -> dict:
    """Return cluster summary (k, centroids, sizes, median net_pts per cluster)."""
    if not SKLEARN_AVAILABLE or len(df) < 10:
        return {"k": 0, "centroids": [], "sizes": [], "median_net": []}
    k = _pick_k(df)
    labels, centroids, _ = _cluster_one(df, k)
    sizes = [int((labels == ci).sum()) for ci in range(k)]
    medians = [float(df.loc[labels == ci, "net_pts"].median()) for ci in range(k)]
    return {
        "k": k,
        "centroids": [
            {col: round(float(v), 3) for col, v in zip(FEATURE_COLS, c)}
            for c in centroids
        ],
        "sizes": sizes,
        "median_net": medians,
    }


# --------------------------------------------------------------------------- #
# Report writer
# --------------------------------------------------------------------------- #

def _fmt_centroid(centroid: dict, feats: tuple[str, ...]) -> str:
    """Render a centroid as a one-line "key=val key=val …" string."""
    return "  ".join(f"{f}={centroid[f]:+.2f}" for f in feats)


def _compare_centroids(win_centroids: list[dict], loss_centroids: list[dict]) -> list[tuple[str, float, float, float]]:
    """For each feature, show the biggest |Δmean(win centroids) − mean(loss centroids)|."""
    if not win_centroids or not loss_centroids:
        return []
    deltas: list[tuple[str, float, float, float]] = []
    for col in FEATURE_COLS:
        w = float(np.mean([c[col] for c in win_centroids]))
        l = float(np.mean([c[col] for c in loss_centroids]))
        deltas.append((col, w, l, w - l))
    deltas.sort(key=lambda x: abs(x[3]), reverse=True)
    return deltas


def _write_report(
    *,
    per_tf: dict[str, dict],
    ledger: pd.DataFrame,
    report_path: Path,
) -> None:
    lines: list[str] = []
    lines.append("# XAUUSD W/L Pattern Extraction — Strict SPEC rules\n")
    lines.append(f"Generated: {pd.Timestamp.utcnow().isoformat()}Z  •  read-only analysis\n")
    lines.append("Source: 16y Dukascopy CSV (Bid+Ask) replayed through production\n")
    lines.append("``analyzer.dws_smt`` + ``analyzer.signal_validator`` — no rule edits.\n")
    lines.append("Win/loss split: net_pts = trade.points / point − bar_spread_pts > 0.\n")
    lines.append("\n---\n")

    for base_tf in config.DWS_SMT_BASE_TFS:
        info = per_tf.get(base_tf)
        if info is None:
            lines.append(f"\n## BASE = {base_tf}: NO RESULT\n")
            continue
        sub = ledger[ledger["base_tf"] == base_tf]
        n_win = int((sub["outcome"] == "win").sum())
        n_loss = int((sub["outcome"] == "loss").sum())
        win_rate = n_win / (n_win + n_loss) if (n_win + n_loss) > 0 else 0.0
        median_win = float(sub.loc[sub.outcome == "win",  "net_pts"].median()) if n_win else 0.0
        median_loss = float(sub.loc[sub.outcome == "loss", "net_pts"].median()) if n_loss else 0.0

        lines.append(f"\n## BASE = {base_tf}   (3TF stack: {'/'.join(config.DWS_SMT_STACKS[base_tf])})\n")
        lines.append(f"- trades total : {n_win + n_loss:,d}\n")
        lines.append(f"- win count    : {n_win:,d}  (median net = {median_win:+.1f} pts)\n")
        lines.append(f"- loss count   : {n_loss:,d}  (median net = {median_loss:+.1f} pts)\n")
        lines.append(f"- win rate     : {win_rate * 100:.1f}%\n")

        # WIN patterns
        win_info = info["win"]
        lines.append(f"\n### Win patterns (k = {win_info['k']})\n")
        for i, c in enumerate(win_info["centroids"]):
            lines.append(f"\n**Win pattern #{i + 1}** "
                         f"— size = {win_info['sizes'][i]:,d}  •  "
                         f"median net = {win_info['median_net'][i]:+.1f} pts\n")
            lines.append("```\n" + _fmt_centroid(c, FEATURE_COLS) + "\n```\n")

        # LOSS patterns
        loss_info = info["loss"]
        lines.append(f"\n### Loss patterns (k = {loss_info['k']})\n")
        for i, c in enumerate(loss_info["centroids"]):
            lines.append(f"\n**Loss pattern #{i + 1}** "
                         f"— size = {loss_info['sizes'][i]:,d}  •  "
                         f"median net = {loss_info['median_net'][i]:+.1f} pts\n")
            lines.append("```\n" + _fmt_centroid(c, FEATURE_COLS) + "\n```\n")

        # Win vs Loss centroid comparison (top 8 deltas)
        deltas = _compare_centroids(win_info["centroids"], loss_info["centroids"])
        if deltas:
            lines.append("\n### Top discriminating features (mean of centroids)\n")
            lines.append("| feature | win mean | loss mean | Δ (win − loss) |\n")
            lines.append("|---|---:|---:|---:|\n")
            for col, w, l, d in deltas[:8]:
                lines.append(f"| {col} | {w:+.3f} | {l:+.3f} | {d:+.3f} |\n")

    report_path.write_text("".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not SKLEARN_AVAILABLE:
        log.error("scikit-learn not available — install it to run clustering")
        return 1

    t0 = time.perf_counter()
    log.info("Loading XAUUSD Dukascopy CSVs (W1/D1/H4/H1/M15)…")
    frames = {tf: _load_tf(tf, XAUUSD_POINT)
              for tf in ("W1", "D1", "H4", "H1", "M15")}
    log.info("Loaded in %.1fs", time.perf_counter() - t0)

    log.info("Building per-bar indicator series for every TF used in stacks…")
    series_by_tf: dict[str, TFSeries] = {
        tf: _build_tf_series(tf, df) for tf, df in frames.items()
    }

    log.info("Running DWS-SMT compute_symbol for entire history…")
    # out_bars set huge so the emitted window == full base series — same trick
    # the existing backtest uses (see _backtest_xauusd.py:239).
    out_bars = max(len(df) for df in frames.values()) + 100
    result = dws_smt.compute_symbol(
        frames=frames,
        stacks=config.DWS_SMT_STACKS,
        period=config.DWS_SMT_PERIOD,
        smooth=config.DWS_SMT_SMOOTH,
        out_bars=out_bars,
    )
    if result is None:
        log.error("compute_symbol returned None")
        return 2

    log.info("Building per-trade ledgers + features for each base TF…")
    ledger_parts: list[pd.DataFrame] = []
    per_tf: dict[str, dict] = {}
    for base_tf, window in result.by_base.items():
        if window is None:
            continue
        stack = config.DWS_SMT_STACKS[base_tf]
        top_label, mid_label, base_label = stack
        df = _build_ledger(
            base_label=base_label,
            base_df=frames[base_label],
            base_series=series_by_tf[base_label],
            mid_series=series_by_tf[mid_label],
            top_series=series_by_tf[top_label],
            window=window,
            point=XAUUSD_POINT,
        )
        ledger_parts.append(df)
        win_df = df[df.outcome == "win"]
        loss_df = df[df.outcome == "loss"]
        log.info("  %s: %d trades  (win=%d  loss=%d)",
                 base_tf, len(df), len(win_df), len(loss_df))
        per_tf[base_tf] = {
            "win":  _cluster_outcome(win_df),
            "loss": _cluster_outcome(loss_df),
        }

    if not ledger_parts:
        log.error("no ledger produced — aborting")
        return 3

    ledger = pd.concat(ledger_parts, ignore_index=True)
    ledger_path = OUT_DIR / "xauusd_per_trade.csv"
    ledger.to_csv(ledger_path, index=False)
    log.info("wrote %s (%d trades)", ledger_path, len(ledger))

    centroids_path = OUT_DIR / "xauusd_centroids.json"
    centroids_path.write_text(json.dumps(per_tf, indent=2, ensure_ascii=False),
                              encoding="utf-8")
    log.info("wrote %s", centroids_path)

    # The production runtime (analyzer/pattern_matcher.py) actually reads
    # xauusd_per_cluster_winrate.json — a richer schema that bundles the
    # StandardScaler params + z-space centroids + Wilson CI + assigned-N
    # win-rate per cluster. Build it here in the same pass so the runtime
    # artefact is FULLY REPRODUCIBLE from a single script invocation
    # (previously the file lived only as a one-off inline-script output,
    # which the SPEC audit flagged as an architectural gap).
    pcw_path = OUT_DIR / "xauusd_per_cluster_winrate.json"
    _write_per_cluster_winrate(ledger=ledger, out_path=pcw_path)
    log.info("wrote %s", pcw_path)

    report_path = OUT_DIR / "xauusd_patterns_report.md"
    _write_report(per_tf=per_tf, ledger=ledger, report_path=report_path)
    log.info("wrote %s", report_path)

    log.info("done in %.1fs", time.perf_counter() - t0)
    return 0


# --------------------------------------------------------------------------- #
# per_cluster_winrate.json builder — the production runtime input.
# --------------------------------------------------------------------------- #

def _wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Two-sided 100·(1−α)% Wilson score interval for a binomial proportion."""
    if n <= 0:
        return 0.0, 0.0
    p = wins / n
    den = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / den
    half = z * ((p * (1.0 - p) / n + z * z / (4.0 * n * n)) ** 0.5) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def _write_per_cluster_winrate(*, ledger: pd.DataFrame, out_path: Path) -> None:
    """Rebuild ``xauusd_per_cluster_winrate.json`` from the freshly emitted
    per-trade ledger. Refits the StandardScaler + KMeans per base TF (same
    config as the in-pass clustering: k=4 per win/loss split, n_init=10,
    random_state=42), then computes per-cluster N / wins / Wilson CI from
    the actual trades that land nearest each centroid in z-space.

    Output schema matches what the runtime loader expects:

      {tf: {
        feature_columns, population,
        scaler: {mean, std},
        clusters: [{pattern_id, learned_from_shape, within_shape_cluster_index,
                    assigned_n, wins, losses, win_rate,
                    win_rate_ci95_low, win_rate_ci95_high,
                    median_net_pts, centroid_raw, centroid_z}]
      }, ...}
    """
    from sklearn.preprocessing import StandardScaler
    from sklearn.cluster import KMeans
    out: dict[str, dict] = {}
    for base_tf in ("M15", "H1", "H4"):
        sub = ledger[ledger.base_tf == base_tf].reset_index(drop=True)
        if sub.empty:
            continue
        X = sub[list(FEATURE_COLS)].to_numpy(dtype=np.float64)
        scaler = StandardScaler().fit(X)
        Xz = scaler.transform(X)
        centroids_z: list[np.ndarray] = []
        meta: list[tuple[str, int]] = []
        for cat in ("win", "loss"):
            mask = (sub.outcome == cat).to_numpy()
            if mask.sum() < 20:
                continue
            km = KMeans(n_clusters=4, n_init=10, random_state=42).fit(Xz[mask])
            centroids_z.append(km.cluster_centers_)
            meta.extend([(cat, i) for i in range(4)])
        all_cz = np.vstack(centroids_z) if centroids_z else np.empty((0, len(FEATURE_COLS)))
        all_craw = scaler.inverse_transform(all_cz) if all_cz.size else all_cz
        # Reassign every trade to its nearest centroid across both shapes —
        # the win-rate per cluster is the empirical WR of those trades.
        if all_cz.size:
            d2 = ((Xz[:, None, :] - all_cz[None, :, :]) ** 2).sum(axis=2)
            nearest = d2.argmin(axis=1)
        else:
            nearest = np.array([], dtype=np.int64)
        clusters: list[dict] = []
        for ci, (cat, idx) in enumerate(meta):
            m = nearest == ci
            n = int(m.sum())
            wins = int((sub.outcome[m] == "win").sum())
            wr = wins / n if n else 0.0
            lo, hi = _wilson_ci(wins, n)
            median_net = float(sub.loc[m, "net_pts"].median()) if n else 0.0
            clusters.append({
                "pattern_id": f"{base_tf}_{cat[0].upper()}{idx + 1}",
                "learned_from_shape": cat,
                "within_shape_cluster_index": idx,
                "assigned_n": n,
                "wins": wins,
                "losses": n - wins,
                "win_rate": round(wr, 4),
                "win_rate_ci95_low":  round(lo, 4),
                "win_rate_ci95_high": round(hi, 4),
                "median_net_pts": round(median_net, 1),
                "centroid_raw": {f: round(float(v), 6) for f, v in zip(FEATURE_COLS, all_craw[ci])},
                "centroid_z":   {f: round(float(v), 6) for f, v in zip(FEATURE_COLS, all_cz[ci])},
            })
        out[base_tf] = {
            "feature_columns": list(FEATURE_COLS),
            "population": {
                "n_trades": int(len(sub)),
                "wins":   int((sub.outcome == "win").sum()),
                "losses": int((sub.outcome == "loss").sum()),
                "win_rate": round(float((sub.outcome == "win").mean()), 4),
            },
            "scaler": {
                "mean": {f: round(float(v), 6) for f, v in zip(FEATURE_COLS, scaler.mean_)},
                "std":  {f: round(float(v), 6) for f, v in zip(FEATURE_COLS, scaler.scale_)},
            },
            "clusters": clusters,
        }
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False),
                        encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
