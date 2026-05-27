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

# Year window — matches scripts/_backtest_all_yearly_swap.py / _generate_oos_baseline.py
# (YEAR_FIRST excludes 2010 because W1 EMA(20) is still warming up that year).
# Without this filter the counts diverge from data/oos_baseline.json by ~7%.
YEAR_FIRST = 2011
YEAR_LAST  = 2025


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
    """Build a per-trade DataFrame for one base TF, with full entry features."""
    spread_pts = base_df["spread"].to_numpy(dtype=np.int64)
    rows: list[dict] = []
    closed = [t for t in window.trades if not t.is_open]

    # `window.times_ms` is the trailing emission; for the full ledger we use
    # the FULL base series ns so trade.entry_idx (offset into the emitted
    # window) is the same index in the full base history (we emit the entire
    # history per the validator config — see backtest script line 239).
    base_full_ns = base_series.times_ns

    for tr in closed:
        if tr.entry_idx >= base_full_ns.size:
            continue
        entry_ns = int(base_full_ns[tr.entry_idx])
        # Year filter — mirror OOS baseline (2011..2025). The 2010 warm-up
        # year is excluded because W1 EMA(20) hasn't stabilised yet — this
        # was the source of a ~7% trade-count overshoot vs the production
        # data/oos_baseline.json baseline.
        entry_year = pd.Timestamp(entry_ns, unit="ns", tz="UTC").year
        if entry_year < YEAR_FIRST or entry_year > YEAR_LAST:
            continue
        feats = _row_for_trade(
            direction=tr.direction,
            entry_ns=entry_ns,
            base=base_series, mid=mid_series, top=top_series,
        )
        if feats is None:
            continue
        net = _net_pts(tr, spread_pts, point)
        rows.append({
            "base_tf": base_label,
            "entry_ts_utc": pd.Timestamp(entry_ns, unit="ns", tz="UTC").isoformat(),
            "direction": int(tr.direction),
            "outcome": "win" if net > 0 else "loss",
            "net_pts": round(net, 2),
            "mae_pts": round(tr.mae / point, 2),
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

    report_path = OUT_DIR / "xauusd_patterns_report.md"
    _write_report(per_tf=per_tf, ledger=ledger, report_path=report_path)
    log.info("wrote %s", report_path)

    log.info("done in %.1fs", time.perf_counter() - t0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
