"""SPEC §12 currency strength + SPEC §12.6 per-pair bias.

Algorithm (SPEC §12.3)
----------------------
1. For each fiat pair, compute the cumulative % change of close over the
   last N *closed* bars of the configured window (one ``TimeframeSpec``
   per SPEC §12.4 window — H1/H4/D1/W1). The in-progress bar is ignored
   so the score never wobbles within a bar; the N-bar span (vs a single
   bar) smooths one-off spikes that would otherwise dominate.
2. For each currency, average all pair changes that contain it. When the
   currency sits in the *base* slot we add the change; when it sits in the
   *quote* slot we subtract it (USDJPY ↑ ⇒ USD stronger, JPY weaker).
3. Z-score normalise the per-currency averages onto a 0–100 scale: the
   cross-sectional mean maps to 50, ±2σ to the 0/100 edges. Unlike a
   min-max scale (which re-stretches strongest→100 / weakest→0 every
   cycle) this keeps a fixed, day-independent meaning, so the ±10/±30
   pair-bias thresholds (SPEC §12.6) stay comparable across cycles and
   windows instead of drifting with the daily volatility range.

XAU (gold) is NOT part of the strength metric — it is not a fiat currency
and its far-larger volatility would distort the cross-sectional scale.

Caching
-------
Each ``compute`` call needs only the last ``N + 2`` bars per (pair,
window) to derive the cumulative closed-bar % change, so the IO is small.
Work is handed to :meth:`MT5Connector.fetch_rates_parallel` for
thread-pool parallelism.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Iterable
import pandas as pd

import config
from analyzer.mt5_connector import MT5Connector

log = logging.getLogger(__name__)

# Z-score → 0..100 mapping factor. ``score = 50 + z * _Z_SCORE_SCALE``
# (clamped to [0, 100]). 25.0 places ±2σ at the 0/100 edges, so the mean
# currency sits exactly at 50 and a currency 1σ above the cross-sectional
# mean is always score 75 — a fixed, volatility-independent meaning.
_Z_SCORE_SCALE: float = 25.0

# Strength is the cumulative % change over the last N CLOSED bars of each
# window's timeframe. N > 1 smooths the single-bar spikes that a 1-bar
# metric over-reacted to (one volatile bar no longer dominates the score),
# while still using closed bars only so there is zero intra-bar wobble.
_STRENGTH_LOOKBACK_BARS: int = 3


# --------------------------------------------------------------------------- #
# Result dataclasses
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CurrencyScore:
    """One currency's strength on the 0–100 scale (SPEC §12.3)."""

    currency: str            # "USD", "EUR", ..., "XAU"
    score: float             # 0..100
    raw_avg: float           # average % change pre-normalisation (signed)
    n_pairs: int             # how many pairs contributed
    is_reference: bool = False   # True for XAU


@dataclass(frozen=True)
class PairBias:
    """SPEC §12.6 per-pair directional bias from currency strength."""

    pair: str                 # e.g. "GBPJPY"
    base: str                 # "GBP"
    quote: str                # "JPY"
    delta: float              # base_score - quote_score
    label: str                # STRONG BUY / BUY / NEUTRAL / SELL / STRONG SELL


@dataclass(frozen=True)
class StrengthWindowResult:
    """All scores + biases for one time window."""

    window: str               # "H1" / "H4" / "D1" / "W1"
    scores: dict[str, CurrencyScore]      # by currency
    pair_biases: dict[str, PairBias]      # by configured display pair (SPEC §7)


@dataclass(frozen=True)
class StrengthSnapshot:
    """The Phase 3 strength output across every configured window."""

    generated_at: float       # epoch seconds (UTC)
    compute_ms: float
    by_window: dict[str, StrengthWindowResult]


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class CurrencyStrengthEngine:
    """Compute currency strength + pair bias for every configured window."""

    def __init__(
        self,
        connector: MT5Connector,
        pairs: Iterable[str] = config.CURRENCY_STRENGTH_PAIRS,
        windows: Iterable[config.TimeframeSpec] = config.STRENGTH_WINDOWS,
        display_pairs: Iterable[str] | None = None,
    ) -> None:
        self._connector = connector
        self._configured_pairs = tuple(pairs)
        self._windows = tuple(windows)
        self._display_pairs = tuple(
            display_pairs if display_pairs is not None
            else (s.base for s in config.SYMBOLS)
        )
        # Set of pairs known to be available on the broker (filled lazily).
        self._available: set[str] = set()

    # ----------------------------------------------------- bootstrap
    def resolve_pairs(self) -> set[str]:
        """Resolve every configured pair on the broker (skipping missing).

        Returns the set of available pairs. Should be called once at
        analysis-loop start-up. Idempotent.
        """
        # XAU excluded from the strength metric → only fiat pairs resolved.
        needed = list(self._configured_pairs)
        resolved = self._connector.resolve_optional(needed)
        self._available = set(resolved.keys()) & set(needed)
        missing = set(self._configured_pairs) - self._available
        if missing:
            log.info("currency_strength: %d of %d pairs unavailable: %s",
                     len(missing), len(self._configured_pairs), sorted(missing))
        return self._available

    # --------------------------------------------------------- compute
    def compute(self) -> StrengthSnapshot:
        """Run a full multi-window pass and return the snapshot."""
        import time
        t0 = time.perf_counter()
        if not self._available:
            self.resolve_pairs()

        usable_pairs = [p for p in self._configured_pairs if p in self._available]
        if not usable_pairs:
            log.warning("currency_strength: no usable pairs on broker")
            return StrengthSnapshot(
                generated_at=time.time(),
                compute_ms=(time.perf_counter() - t0) * 1000.0,
                by_window={},
            )

        # Fetch 3 closes per (pair, window) in one batched call so the
        # engine can compare the last two *closed* bars (close[-3]→[-2]),
        # ignoring the in-progress bar. Windows are TimeframeSpec instances
        # (bars_to_fetch=3) feeding ``fetch_rates_parallel`` with no adapter.
        # XAU is not fetched here — it is excluded from the strength metric.
        rates = self._connector.fetch_rates_parallel(
            list(usable_pairs), self._windows
        )

        by_window: dict[str, StrengthWindowResult] = {}
        for window in self._windows:
            scores = self._compute_window(window, usable_pairs, rates)
            biases = self._compute_pair_biases(scores)
            by_window[window.label] = StrengthWindowResult(
                window=window.label, scores=scores, pair_biases=biases,
            )

        return StrengthSnapshot(
            generated_at=time.time(),
            compute_ms=(time.perf_counter() - t0) * 1000.0,
            by_window=by_window,
        )

    # --------------------------------------------------- per-window calc
    def _compute_window(
        self,
        window: config.TimeframeSpec,
        usable_pairs: list[str],
        rates: dict[tuple[str, str], pd.DataFrame],
    ) -> dict[str, CurrencyScore]:
        # Collect pct changes per pair for this window.
        # Use the LAST FULLY-CLOSED bar's close as ``cur`` (df.iloc[-2]) and
        # the bar before that (df.iloc[-3]) as ``prev``. Previously we used
        # the *in-progress* bar's close (iloc[-1] = current price), which made
        # the score wobble every tick — on a fresh H1 bar the % change was
        # tiny one second then huge the next. Closed-to-closed is stable
        # within the bar and only updates when a new bar prints.
        pct: dict[str, float] = {}
        # Need: endpoint close[-2] (last closed bar) + reference N bars
        # before it = close[-2-N]  ⇒  len(df) >= N + 2.
        need = _STRENGTH_LOOKBACK_BARS + 2
        for pair in usable_pairs:
            df = rates.get((pair, window.label))
            if df is None or len(df) < need:
                continue
            closes = df["close"]
            # Cumulative % change over the last N CLOSED bars: reference is
            # close[-2-N], endpoint is close[-2]. The in-progress bar[-1]
            # is ignored (no tick wobble); N bars of span smooth spikes.
            ref = float(closes.iloc[-2 - _STRENGTH_LOOKBACK_BARS])
            cur = float(closes.iloc[-2])
            # MT5 can return NaN/Inf for missing bars (W1 week-start,
            # holidays). ``ref == 0`` alone does NOT catch NaN — an
            # unguarded NaN poisons the per-currency average and collapses
            # the whole window's score scale.
            if not (math.isfinite(ref) and math.isfinite(cur)) or ref == 0:
                continue
            pct[pair] = (cur - ref) / ref * 100.0

        # SPEC §12.3 step 2: average % change per currency.
        sum_by: dict[str, float] = {ccy: 0.0 for ccy in config.ALL_STRENGTH_CURRENCIES}
        cnt_by: dict[str, int] = {ccy: 0 for ccy in config.ALL_STRENGTH_CURRENCIES}
        for pair, change in pct.items():
            base, quote = self._split_pair(pair)
            if base in sum_by:
                sum_by[base] += change
                cnt_by[base] += 1
            if quote in sum_by:
                sum_by[quote] += -change   # USD↑ means JPY↓ for USDJPY
                cnt_by[quote] += 1

        avgs: dict[str, float] = {}
        for ccy in config.ALL_STRENGTH_CURRENCIES:
            if cnt_by[ccy] > 0:
                avgs[ccy] = sum_by[ccy] / cnt_by[ccy]

        # XAU is INTENTIONALLY EXCLUDED from the strength metric.
        # Gold's volatility is several times larger than any fiat's, so
        # feeding it into the shared cross-sectional scale dominated the
        # std/range and squeezed every fiat score toward the centre —
        # making the meter useless. Strength is now a pure 7-fiat
        # cross-pair metric (SPEC §12.1: "XAUは計算に含めない").
        if len(avgs) < 2:
            # Need at least two currencies for a meaningful z-score scale.
            return {}

        # SPEC §12.3 step 3: z-score normalise → 0..100 over fiat only.
        return self._normalise(avgs, cnt_by)

    # ------------------------------------------------ pair-bias matrix
    def _compute_pair_biases(
        self, scores: dict[str, CurrencyScore],
    ) -> dict[str, PairBias]:
        out: dict[str, PairBias] = {}
        for pair in self._display_pairs:
            try:
                base, quote = self._split_pair(pair)
            except ValueError:
                continue
            # XAU is not in ``scores`` (excluded from strength), so a pair
            # like XAUUSD simply yields no bias — base_s stays None.
            base_s = scores.get(base)
            quote_s = scores.get(quote)
            if base_s is None or quote_s is None:
                continue
            delta = base_s.score - quote_s.score
            label = self._classify_bias(delta)
            out[pair] = PairBias(pair=pair, base=base, quote=quote,
                                 delta=delta, label=label)
        return out

    # ---------------------------------------------------- helpers
    @staticmethod
    def _split_pair(pair: str) -> tuple[str, str]:
        """Split a 6-or-7-char pair into (base, quote).

        Handles ``XAUUSD`` (XAU+USD) as well as ``EURUSD``/``USDJPY``.
        """
        if len(pair) == 6:
            return pair[:3], pair[3:]
        if len(pair) == 7 and pair.startswith("XAU"):
            return "XAU", pair[3:]
        raise ValueError(f"unrecognised pair shape: {pair!r}")

    @staticmethod
    def _normalise(avgs: dict[str, float], cnt: dict[str, int]) -> dict[str, CurrencyScore]:
        """Z-score scale the raw averages onto 0..100; mean currency = 50.

        ``score = 50 + ((raw − mean) / std) · _Z_SCORE_SCALE`` clamped to
        [0, 100]. When every currency has the same average (std == 0) all
        scores collapse to the 50 midline. This replaces the old min-max
        scaling, which re-stretched the strongest/weakest to 100/0 every
        cycle and made the SPEC §12.6 ±10/±30 thresholds meaningless.
        """
        vals = list(avgs.values())
        n = len(vals)
        mean = sum(vals) / n
        variance = sum((v - mean) ** 2 for v in vals) / n
        std = variance ** 0.5
        out: dict[str, CurrencyScore] = {}
        for ccy, raw in avgs.items():
            if std > 0:
                z = (raw - mean) / std
                score = 50.0 + z * _Z_SCORE_SCALE
                score = max(0.0, min(100.0, score))
            else:
                score = 50.0
            out[ccy] = CurrencyScore(
                currency=ccy, score=score, raw_avg=raw,
                n_pairs=cnt[ccy], is_reference=False,
            )
        return out

    @staticmethod
    def _classify_bias(delta: float) -> str:
        """Bucket *delta* against SPEC §12.6 thresholds.

        SPEC text places ±10 inside the NEUTRAL band (-10〜+10), so we use
        strict inequality for the weak/strong thresholds — Δ == 10 ⇒
        NEUTRAL, Δ == 10.0001 ⇒ BUY.
        """
        if delta >= config.STRENGTH_PAIR_BIAS_STRONG:
            return "STRONG BUY"
        if delta > config.STRENGTH_PAIR_BIAS_WEAK:
            return "BUY"
        if delta <= -config.STRENGTH_PAIR_BIAS_STRONG:
            return "STRONG SELL"
        if delta < -config.STRENGTH_PAIR_BIAS_WEAK:
            return "SELL"
        return "NEUTRAL"

