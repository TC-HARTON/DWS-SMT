"""SPEC §13 correlation matrix.

Computes pairwise Pearson correlation between the 10 monitored symbols'
H1 close-to-close returns over each of the SPEC §13.4 windows (20, 100,
500 bars). The output is a square symmetric matrix per window plus a
single ``CorrelationSnapshot`` carrying all three.

Why correlations on returns (not raw close)?
-------------------------------------------
Raw close-on-close prices for FX pairs have very different scales and
strong autocorrelation; computing correlations on **% returns** isolates
the co-movement that traders actually care about (when GBPJPY moves
0.5 %, does GBPUSD move with it?). Returns are stationary enough that
the Pearson coefficient is well-defined.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd

import config
from analyzer.mt5_connector import MT5Connector

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CorrelationMatrix:
    """One window's pairwise-Pearson matrix."""

    bars: int                          # 20 / 100 / 500
    symbols: tuple[str, ...]           # rows / cols, in the SPEC §7.1 order
    matrix: np.ndarray                 # shape (n, n), float64, 1.0 on diagonal


@dataclass(frozen=True)
class CorrelationSnapshot:
    """Bundle of all configured correlation windows."""

    generated_at: float                # epoch seconds (UTC)
    compute_ms: float
    timeframe: str                     # e.g. "H1"
    by_window: dict[int, CorrelationMatrix]


class CorrelationEngine:
    """Pull H1 closes for every configured symbol and compute correlations."""

    def __init__(
        self,
        connector: MT5Connector,
        windows: tuple[int, ...] = config.CORRELATION_WINDOWS_BARS,
        timeframe_label: str = config.CORRELATION_TIMEFRAME,
    ) -> None:
        self._connector = connector
        self._windows = tuple(sorted(set(windows)))
        self._tf_label = timeframe_label
        self._tf = config.TIMEFRAME_BY_LABEL.get(timeframe_label)
        if self._tf is None:
            raise ValueError(
                f"correlation TF {timeframe_label!r} not found in config.TIMEFRAMES"
            )

    def compute(self) -> CorrelationSnapshot:
        t0 = time.perf_counter()
        # We need enough bars for the longest window; +1 because returns
        # consume one bar.
        max_bars = max(self._windows) + 1
        symbols = [s.base for s in config.SYMBOLS]

        # Override bars_to_fetch on the configured TF without mutating it.
        from config import TimeframeSpec
        fetch_spec = TimeframeSpec(
            label=self._tf.label,
            mt5_const=self._tf.mt5_const,
            ema_period=self._tf.ema_period,
            bars_to_fetch=max_bars,
        )
        rates = self._connector.fetch_rates_parallel(symbols, [fetch_spec])

        # Build a single DataFrame: rows = bar time, cols = symbol.
        series_by_sym: dict[str, pd.Series] = {}
        for sym in symbols:
            df = rates.get((sym, self._tf.label))
            if df is None or df.empty:
                continue
            series_by_sym[sym] = df["close"].astype(np.float64)
        if not series_by_sym:
            log.warning("correlation: no rate data for any symbol")
            return CorrelationSnapshot(
                generated_at=time.time(),
                compute_ms=(time.perf_counter() - t0) * 1000.0,
                timeframe=self._tf.label, by_window={},
            )

        closes = pd.DataFrame(series_by_sym).dropna(how="all")
        # Bar timestamps differ slightly across symbols on some brokers; use
        # the intersection so every column shares the same row index.
        returns = closes.pct_change().dropna()
        present_symbols = tuple(returns.columns)

        by_window: dict[int, CorrelationMatrix] = {}
        for window in self._windows:
            if len(returns) < window:
                log.info(
                    "correlation: only %d bars available, skipping window=%d",
                    len(returns), window,
                )
                continue
            slice_ = returns.tail(window)
            # `numpy.corrcoef` handles columns-as-variables when transposed.
            matrix = np.corrcoef(slice_.to_numpy(), rowvar=False)
            # A constant (zero-variance) series produces a NaN row/column.
            # Force exact 1.0 on the diagonal first, then replace any
            # remaining NaN with 0 so the heatmap renders a neutral cell
            # instead of a transparent gap that confuses the user.
            np.fill_diagonal(matrix, 1.0)
            if np.isnan(matrix).any():
                nan_cols = np.where(np.isnan(matrix).all(axis=0))[0]
                if len(nan_cols):
                    log.info(
                        "correlation: %d constant series in window=%d (%s)",
                        len(nan_cols), window,
                        ", ".join(present_symbols[i] for i in nan_cols),
                    )
                matrix = np.nan_to_num(matrix, nan=0.0)
                np.fill_diagonal(matrix, 1.0)
            by_window[window] = CorrelationMatrix(
                bars=window, symbols=present_symbols, matrix=matrix,
            )

        return CorrelationSnapshot(
            generated_at=time.time(),
            compute_ms=(time.perf_counter() - t0) * 1000.0,
            timeframe=self._tf.label, by_window=by_window,
        )
