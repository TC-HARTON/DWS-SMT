"""Runtime pattern matcher — surfaces "this current setup looks like
pattern X, which had historical win rate Y%" on the dashboard.

The centroid + win-rate table is produced offline by
``scripts/_extract_xauusd_patterns.py`` and stored in
``data/loss_analysis/xauusd_per_cluster_winrate.json``. This module loads
that table at startup, computes a 22-feature vector from the live OHLC
frames at each analysis cycle, and finds the nearest centroid in the
same z-space the offline script used.

The table includes the StandardScaler mean/std per feature so the live
input gets standardised IDENTICALLY to the training pass — no sklearn
needed at runtime, just numpy. The matcher reports:

* the pattern id (e.g. M15_W4)
* the empirical historical win rate of trades that fell into that cell
* the 95% Wilson interval of that win rate
* the trades-assigned sample size
* a confidence flag derived from (sample size, CI width, walk-forward
  stability — pre-judged once and recorded in the JSON)

Currently only XAUUSD has a centroid table. Other symbols return None.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

import config
from analyzer import indicators

log = logging.getLogger(__name__)

# Project root → data/loss_analysis/...
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TABLE_FILE = _PROJECT_ROOT / "data" / "loss_analysis" / "xauusd_per_cluster_winrate.json"

# Bars before entry used for the slope feature — must match the offline
# extractor's SLOPE_LOOKBACK constant exactly.
SLOPE_LOOKBACK = 5

# Walk-forward stability verdicts, hand-encoded from the analysis report.
# These map (base_tf, pattern_id) -> reliability tier shown on the panel.
# M15 is fully stable; H1 has two unstable win patterns; H4 is too small.
_RELIABILITY: dict[str, dict[str, str]] = {
    "M15": {pid: "高" for pid in (
        "M15_W1", "M15_W2", "M15_W3", "M15_W4",
        "M15_L1", "M15_L2", "M15_L3", "M15_L4",
    )},
    "H1": {
        "H1_W1": "中", "H1_W2": "低", "H1_W3": "低", "H1_W4": "中",
        "H1_L1": "中", "H1_L2": "中", "H1_L3": "低", "H1_L4": "中",
    },
    "H4": {pid: "低" for pid in (
        "H4_W1", "H4_W2", "H4_W3", "H4_W4",
        "H4_L1", "H4_L2", "H4_L3", "H4_L4",
    )},
}


# --------------------------------------------------------------------------- #
# Result dataclass
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PatternMatch:
    """One pattern match for a given (symbol, base_tf, side) live setup."""

    pattern_id: str
    learned_from_shape: str           # "win" or "loss"
    win_rate: float                   # 0..1, empirical historical rate
    win_rate_ci_low: float
    win_rate_ci_high: float
    sample_n: int                     # trades assigned to this centroid
    median_net_pts: float
    distance_z: float                 # euclidean distance in z-space
    reliability: str                  # "高" / "中" / "低"


@dataclass(frozen=True)
class SymbolPatternMatches:
    """All matches for a symbol — one per base TF when a trigger side is set."""

    by_base: dict[str, PatternMatch | None] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class PatternMatcher:
    """Load the centroid table once and reuse for every analysis cycle."""

    def __init__(self, table_file: Path = _TABLE_FILE) -> None:
        self._table: dict | None = None
        self._features: tuple[str, ...] = ()
        # Vectorised arrays per base TF: mean, std, centroid matrix in z-space.
        self._mean:    dict[str, np.ndarray] = {}
        self._std:     dict[str, np.ndarray] = {}
        self._centz:   dict[str, np.ndarray] = {}
        self._cluster_meta: dict[str, list[dict]] = {}

        if not table_file.exists():
            log.info("pattern_matcher: %s not found — matcher disabled", table_file)
            return
        try:
            self._table = json.loads(table_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("pattern_matcher: failed to read %s: %s", table_file, exc)
            return

        # Same feature column order across every TF (sanity-asserted).
        any_tf = next(iter(self._table.values()))
        self._features = tuple(any_tf["feature_columns"])

        for tf, tf_data in self._table.items():
            cols = tuple(tf_data["feature_columns"])
            if cols != self._features:
                log.warning("pattern_matcher: feature order mismatch on %s", tf)
                continue
            sc = tf_data["scaler"]
            self._mean[tf] = np.array([sc["mean"][f] for f in cols], dtype=np.float64)
            self._std[tf]  = np.array([sc["std"][f]  for f in cols], dtype=np.float64)
            self._std[tf]  = np.where(self._std[tf] == 0.0, 1.0, self._std[tf])
            centz = np.array(
                [[c["centroid_z"][f] for f in cols] for c in tf_data["clusters"]],
                dtype=np.float64,
            )
            self._centz[tf] = centz
            self._cluster_meta[tf] = tf_data["clusters"]

        log.info("pattern_matcher: loaded %d base-TF tables (%d features)",
                 len(self._centz), len(self._features))

    @property
    def enabled(self) -> bool:
        return bool(self._centz)

    @property
    def supported_symbols(self) -> tuple[str, ...]:
        # Currently only XAUUSD has an extracted table. Hardcoded — adding a
        # new symbol means extending data/loss_analysis/<sym>_per_cluster_winrate.json
        # and listing it here.
        return ("XAUUSD",)

    # ------------------------------------------------------------- feature
    @staticmethod
    def _feature_row(
        *,
        direction: int,
        base_tf: str,
        top_df: pd.DataFrame, mid_df: pd.DataFrame, base_df: pd.DataFrame,
    ) -> dict[str, float] | None:
        """Compute the 22-feature vector at the LATEST closed bar of each TF.

        Must produce a vector that is *bit-comparable* to the one the offline
        extractor builds at a historical trade entry — same indicator
        formulas, same direction-signing, same fallbacks.
        """
        def _last_idx(df: pd.DataFrame) -> int:
            # Use the most recent CLOSED bar (skip the still-forming bar).
            n = len(df)
            return n - 2 if n >= 2 else -1

        bi = _last_idx(base_df)
        mi = _last_idx(mid_df)
        ti = _last_idx(top_df)
        if bi < SLOPE_LOOKBACK + 1 or mi < 0 or ti < 0:
            return None

        def _ind(df: pd.DataFrame):
            close = df["close"].to_numpy(dtype=np.float64)
            high  = df["high"].to_numpy(dtype=np.float64)
            low   = df["low"].to_numpy(dtype=np.float64)
            ema20 = indicators.ema(close, 20)
            ema50 = indicators.ema(close, 50)
            rsi14 = indicators.rsi(close, 14)
            atr14 = indicators.atr(high, low, close, 14)
            adx14, di_p, di_m = indicators.adx(high, low, close, 14)
            return close, high, low, ema20, ema50, rsi14, atr14, adx14, di_p, di_m

        bc, bh, bl, b_e20, b_e50, b_rsi, b_atr, b_adx, b_dp, b_dm = _ind(base_df)
        mc, _, _, m_e20, m_e50, m_rsi, m_atr, m_adx, m_dp, m_dm = _ind(mid_df)
        tc, _, _, t_e20, t_e50, t_rsi, t_atr, t_adx, t_dp, t_dm = _ind(top_df)

        def _safe(v: float) -> float:
            if v is None or math.isnan(v) or math.isinf(v):
                return 0.0
            return float(v)

        def _di_diff(p, m, i): return _safe(p[i] - m[i]) * direction
        def _ema_dist(close, ema, atr, i):
            if atr[i] <= 0 or math.isnan(atr[i]): return 0.0
            return _safe((close[i] - ema[i]) / atr[i]) * direction
        def _close_vs_ema50(close, ema50, i): return _safe(close[i] - ema50[i]) * direction

        # Recent slope on base TF
        atr_b = b_atr[bi]
        sl_end = bc[bi]; sl_beg = bc[bi - SLOPE_LOOKBACK]
        slope = ((sl_end - sl_beg) / atr_b) * direction if atr_b and atr_b > 0 else 0.0

        # ATR percentile vs trailing 90 days
        bars_per_day = {"H4": 6, "H1": 24, "M15": 96}[base_tf]
        window = bars_per_day * 90
        lo = max(0, bi - window)
        atr_hist = b_atr[lo: bi + 1]
        atr_hist = atr_hist[~np.isnan(atr_hist)]
        atr_pct_90d = float((atr_hist < atr_b).mean()) if atr_hist.size > 5 and atr_b > 0 else 0.5

        entry_ts = pd.Timestamp(base_df.index[bi]).tz_convert("Asia/Tokyo") \
            if base_df.index.tz else \
            pd.Timestamp(base_df.index[bi], tz="UTC").tz_convert("Asia/Tokyo")

        return {
            "base_rsi":           _safe(b_rsi[bi]),
            "base_adx":           _safe(b_adx[bi]),
            "base_di_diff":       _di_diff(b_dp, b_dm, bi),
            "base_atr_pct":       _safe(atr_b / bc[bi] * 100.0) if bc[bi] else 0.0,
            "base_ema_dist":      _ema_dist(bc, b_e20, b_atr, bi),
            "base_ema_slope":     _safe(slope),
            "base_close_vs_ema50": _close_vs_ema50(bc, b_e50, bi),

            "mid_rsi":           _safe(m_rsi[mi]),
            "mid_adx":           _safe(m_adx[mi]),
            "mid_di_diff":       _di_diff(m_dp, m_dm, mi),
            "mid_atr_pct":       _safe(m_atr[mi] / mc[mi] * 100.0) if mc[mi] else 0.0,
            "mid_ema_dist":      _ema_dist(mc, m_e20, m_atr, mi),
            "mid_close_vs_ema50": _close_vs_ema50(mc, m_e50, mi),

            "top_rsi":           _safe(t_rsi[ti]),
            "top_adx":           _safe(t_adx[ti]),
            "top_di_diff":       _di_diff(t_dp, t_dm, ti),
            "top_ema_dist":      _ema_dist(tc, t_e20, t_atr, ti),
            "top_close_vs_ema50": _close_vs_ema50(tc, t_e50, ti),

            "hour_jst":          float(entry_ts.hour),
            "dow":               float(entry_ts.dayofweek),
            "atr_pct_90d":       atr_pct_90d,
        }

    # -------------------------------------------------------------- match
    def match(
        self,
        *,
        symbol: str,
        base_tf: str,
        direction: int,
        frames: dict[str, pd.DataFrame],
    ) -> PatternMatch | None:
        """Compute the nearest pattern centroid for one (sym, base_tf, side)."""
        if symbol not in self.supported_symbols:
            return None
        if base_tf not in self._centz:
            return None
        if direction not in (1, -1):
            return None

        # Resolve the 3-TF stack for this base TF (top → mid → base).
        stack = config.DWS_SMT_STACKS.get(base_tf)
        if not stack or len(stack) != 3:
            return None
        top_label, mid_label, base_label = stack
        if not all(label in frames for label in (top_label, mid_label, base_label)):
            return None

        feats = self._feature_row(
            direction=direction,
            base_tf=base_tf,
            top_df=frames[top_label],
            mid_df=frames[mid_label],
            base_df=frames[base_label],
        )
        if feats is None:
            return None

        # Standardise with the saved scaler (mean / std per feature).
        x = np.array([feats[c] for c in self._features], dtype=np.float64)
        xz = (x - self._mean[base_tf]) / self._std[base_tf]

        centz = self._centz[base_tf]
        dists = np.sqrt(((centz - xz[None, :]) ** 2).sum(axis=1))
        ci = int(dists.argmin())
        meta = self._cluster_meta[base_tf][ci]

        rel_map = _RELIABILITY.get(base_tf, {})
        reliability = rel_map.get(meta["pattern_id"], "中")

        return PatternMatch(
            pattern_id=meta["pattern_id"],
            learned_from_shape=meta["learned_from_shape"],
            win_rate=float(meta["win_rate"]),
            win_rate_ci_low=float(meta["win_rate_ci95_low"]),
            win_rate_ci_high=float(meta["win_rate_ci95_high"]),
            sample_n=int(meta["assigned_n"]),
            median_net_pts=float(meta["median_net_pts"]),
            distance_z=float(dists[ci]),
            reliability=reliability,
        )

    def match_symbol(
        self,
        *,
        symbol: str,
        bias_by_base: dict[str, int],
        frames: dict[str, pd.DataFrame],
    ) -> SymbolPatternMatches | None:
        """Match every base TF the caller knows the trade direction for.

        ``bias_by_base`` maps base_tf -> +1 / -1 / 0 (no direction). 0 entries
        are skipped — pattern semantics are direction-signed.
        """
        if symbol not in self.supported_symbols or not self.enabled:
            return None
        out: dict[str, PatternMatch | None] = {}
        for base_tf, side in bias_by_base.items():
            if side not in (1, -1):
                out[base_tf] = None
                continue
            out[base_tf] = self.match(
                symbol=symbol, base_tf=base_tf,
                direction=side, frames=frames,
            )
        return SymbolPatternMatches(by_base=out)
