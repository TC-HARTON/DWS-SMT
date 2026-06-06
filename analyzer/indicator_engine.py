"""High-level indicator computation: rates DataFrame → numeric snapshots.

This sits between :mod:`analyzer.mt5_connector` (raw OHLC) and
:mod:`analyzer.state` (the cached snapshot read by the dashboard). It is
deliberately stateless and side-effect free so it can be unit-tested
without touching MetaTrader 5.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterable

import numpy as np
import pandas as pd

import config
from analyzer import indicators

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Result dataclasses
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TimeframeIndicators:
    """Per (symbol, timeframe) computed indicator snapshot."""

    label: str               # "D1" / "H4" / "H1" / "M15"
    last_close: float
    ema: float | None
    ema_period: int
    above_ema: bool | None   # last_close > ema
    rsi: float | None
    atr: float | None
    adx: float | None
    di_plus: float | None
    di_minus: float | None
    bar_time: pd.Timestamp   # time of the *closed* bar used for the latest value


@dataclass(frozen=True)
class ChartBars:
    """Compact OHLC payload for clientside candlestick / sparkline drawing.

    ``ohlc_h4`` carries the most recent N H4 bars (one tuple per bar:
    open / high / low / close) for the XL panel chart. ``closes_m15``
    carries just the close prices needed for MD/SM sparklines.
    """

    ohlc_h4: tuple[tuple[float, float, float, float], ...]
    closes_m15: tuple[float, ...]
    ema_h4: float | None     # last EMA value so the chart can draw a horizontal


@dataclass(frozen=True)
class SymbolIndicators:
    """All timeframe snapshots for a single symbol."""

    base: str                 # SPEC symbol name
    broker_name: str
    by_tf: dict[str, TimeframeIndicators]
    chart: ChartBars | None = None    # Phase 6: lightweight OHLC for the UI


@dataclass(frozen=True)
class AnalysisSnapshot:
    """Result of one full indicator pass over every (symbol, TF)."""

    generated_at: pd.Timestamp  # UTC
    compute_ms: float           # SPEC 19 target: ≤50 ms
    by_symbol: dict[str, SymbolIndicators]


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class IndicatorEngine:
    """Compute SPEC §6 indicators from rate DataFrames keyed by (base, tf_label).

    Batching strategy
    -----------------
    Per cycle we group rates by timeframe, stack every symbol's
    high/low/close into ``(n_symbols, n_bars)`` matrices, and call each
    indicator once per timeframe. With 10 symbols × 4 TFs that turns
    ``~1600`` ``scipy.signal.lfilter`` invocations into ``~16``; profile
    data showed the per-call Python overhead — not the C work — was the
    dominant cost, so batching collapses cycle time well under the
    SPEC §19 50 ms budget.
    """

    def __init__(
        self,
        symbols: Iterable[config.SymbolSpec] = config.SYMBOLS,
        timeframes: Iterable[config.TimeframeSpec] = config.TIMEFRAMES,
        rsi_period: int = config.RSI_PERIOD,
        atr_period: int = config.ATR_PERIOD,
        adx_period: int = config.ADX_PERIOD,
    ) -> None:
        self._symbols = tuple(symbols)
        self._timeframes = tuple(timeframes)
        self._rsi_period = rsi_period
        self._atr_period = atr_period
        self._adx_period = adx_period

    # ------------------------------------------------------------ per-TF batch
    def _compute_timeframe_batch(
        self,
        tf: config.TimeframeSpec,
        members: list[tuple[str, pd.DataFrame]],
    ) -> dict[str, TimeframeIndicators]:
        """Compute indicators for every (base, tf) pair sharing the same length.

        Args:
            tf: timeframe being batched.
            members: ``(base, df)`` tuples, all sharing identical bar count.

        Returns:
            ``{base: TimeframeIndicators}`` for this timeframe.
        """
        if not members:
            return {}
        bases = [b for b, _ in members]
        dfs = [df for _, df in members]
        # All DataFrames must agree on bar count for batched stacking.
        high = np.vstack([df["high"].to_numpy(dtype=np.float64) for df in dfs])
        low = np.vstack([df["low"].to_numpy(dtype=np.float64) for df in dfs])
        close = np.vstack([df["close"].to_numpy(dtype=np.float64) for df in dfs])
        # Accuracy: report the last *closed* bar, never the in-progress one, so
        # the indicators do not repaint tick-by-tick. The final column is the
        # forming bar — every read below drops it.
        if close.shape[1] < 2:
            return {}

        ema_arr = indicators.ema(close, tf.ema_period)                       # (k, n)
        rsi_arr = indicators.rsi(close, self._rsi_period)                    # (k, n)
        atr_arr = indicators.atr(high, low, close, self._atr_period)         # (k, n)
        adx_arr, dip_arr, dim_arr = indicators.adx(
            high, low, close, self._adx_period
        )

        result: dict[str, TimeframeIndicators] = {}
        # ``[:-1]`` / ``[-2]`` drop the forming bar so every value is the last
        # closed bar — the indicators no longer shift within a bar.
        for i, base in enumerate(bases):
            ema_val = indicators.last_finite(ema_arr[i][:-1])
            last_close = float(close[i, -2])
            above_ema = (last_close > ema_val) if ema_val is not None else None
            bar_time = dfs[i].index[-2]
            if bar_time.tz is not None:
                bar_time = bar_time.tz_convert("UTC")
            result[base] = TimeframeIndicators(
                label=tf.label,
                last_close=last_close,
                ema=ema_val,
                ema_period=tf.ema_period,
                above_ema=above_ema,
                rsi=indicators.last_finite(rsi_arr[i][:-1]),
                atr=indicators.last_finite(atr_arr[i][:-1]),
                adx=indicators.last_finite(adx_arr[i][:-1]),
                di_plus=indicators.last_finite(dip_arr[i][:-1]),
                di_minus=indicators.last_finite(dim_arr[i][:-1]),
                bar_time=bar_time,
            )
        return result

    # ------------------------------------------------------------ pass over all
    def compute(self, rates: dict[tuple[str, str], pd.DataFrame]) -> AnalysisSnapshot:
        """Compute indicators for every configured (symbol, TF) in batched fashion.

        Missing pairs (the rate fetch returned nothing) are skipped rather
        than raising, so a transient MT5 hiccup never blanks the entire
        dashboard. If two symbols in the same TF have differing bar counts
        they fall into separate batches so the underlying ``np.vstack`` does
        not raise.

        Args:
            rates: ``{(base, tf_label): DataFrame}`` as produced by
                :meth:`MT5Connector.fetch_rates_parallel`.

        Returns:
            :class:`AnalysisSnapshot` with the total compute time measured.
        """
        t0 = time.perf_counter()
        symbol_tfs: dict[str, dict[str, TimeframeIndicators]] = {
            s.base: {} for s in self._symbols
        }

        for tf in self._timeframes:
            # Group members by bar count so np.vstack never raises.
            by_len: dict[int, list[tuple[str, pd.DataFrame]]] = {}
            for sym in self._symbols:
                df = rates.get((sym.base, tf.label))
                if df is None or df.empty:
                    continue
                by_len.setdefault(len(df), []).append((sym.base, df))
            for members in by_len.values():
                try:
                    batch_result = self._compute_timeframe_batch(tf, members)
                except Exception:  # noqa: BLE001 — log and continue with next batch
                    log.exception(
                        "indicator batch failed for tf=%s symbols=%s",
                        tf.label, [b for b, _ in members],
                    )
                    continue
                for base, ind in batch_result.items():
                    symbol_tfs[base][tf.label] = ind

        out = {
            base: SymbolIndicators(
                base=base, broker_name=base, by_tf=per_tf,
                chart=self._extract_chart_bars(base, per_tf, rates),
            )
            for base, per_tf in symbol_tfs.items()
            if per_tf
        }

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if elapsed_ms > config.TARGET_ANALYSIS_BUDGET_MS:
            log.warning(
                "indicator compute exceeded SPEC §19 budget: %.1f ms > %d ms (symbols=%d)",
                elapsed_ms, config.TARGET_ANALYSIS_BUDGET_MS, len(out),
            )
        return AnalysisSnapshot(
            generated_at=pd.Timestamp.now("UTC"),
            compute_ms=elapsed_ms,
            by_symbol=out,
        )

    @staticmethod
    def _extract_chart_bars(
        base: str,
        per_tf: dict[str, TimeframeIndicators],
        rates: dict[tuple[str, str], pd.DataFrame],
    ) -> ChartBars | None:
        """Pull last 40 H4 OHLC + last 30 M15 closes for the clientside charts."""
        h4_df = rates.get((base, "H4"))
        m15_df = rates.get((base, "M15"))
        ohlc_h4: tuple = ()
        if h4_df is not None and not h4_df.empty:
            tail = h4_df.tail(40)
            ohlc_h4 = tuple(
                (float(o), float(h), float(l), float(c))
                for o, h, l, c in zip(
                    tail["open"], tail["high"], tail["low"], tail["close"],
                )
            )
        closes_m15: tuple = ()
        if m15_df is not None and not m15_df.empty:
            closes_m15 = tuple(float(v) for v in m15_df["close"].tail(30))
        h4_ind = per_tf.get("H4")
        ema_h4 = h4_ind.ema if h4_ind else None
        if not ohlc_h4 and not closes_m15:
            return None
        return ChartBars(ohlc_h4=ohlc_h4, closes_m15=closes_m15, ema_h4=ema_h4)

    @staticmethod
    def with_broker_names(
        snapshot: AnalysisSnapshot, broker_map: dict[str, str]
    ) -> AnalysisSnapshot:
        """Return a copy of *snapshot* with broker names spliced in.

        Done as a separate step so the engine has no dependency on the
        connector and stays test-friendly.
        """
        patched = {
            base: SymbolIndicators(
                base=sym.base,
                broker_name=broker_map.get(base, sym.base),
                by_tf=sym.by_tf,
                chart=sym.chart,
            )
            for base, sym in snapshot.by_symbol.items()
        }
        return AnalysisSnapshot(
            generated_at=snapshot.generated_at,
            compute_ms=snapshot.compute_ms,
            by_symbol=patched,
        )

