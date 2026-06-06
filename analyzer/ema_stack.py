"""EMA-stack oscillator — a repaint-free single-series trend read for XAUUSD.

Three EMAs on the M15 CLOSED-bar series: EMA20 (M15), EMA80 (≈1H EMA20), EMA320
(≈4H EMA20). EMA320 is the trend centerline; price / EMA80 / EMA20 are expressed
as percent deviation from EMA320, so they oscillate above/below a flat zero
center (RSI-style). Above the center is an uptrend, below is a downtrend — the
read is left to the user; this module computes **no trigger**.

Repaint-free by construction
----------------------------
Each EMA is causal (a bar's value depends only on closes up to and including
that bar) and only CONFIRMED bars are used — the still-forming last bar is
dropped. A confirmed bar's three EMA values therefore never change as later bars
arrive. There is no multi-timeframe mapping anywhere, which is the only place
look-ahead could enter; collapsing the 3-TF idea onto one M15 series via period
multiples (20 / 80 / 320) is precisely what removes that risk.

Display-only context — never feeds trigger / trade / order logic. ``compute_ema_stack``
is defensive: it returns a stale snapshot rather than raising into the analysis loop.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

import config
from analyzer.disparity_bands import load_bands

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmaStackSnapshot:
    """One EMA-stack refresh: the three EMAs + their %-deviation series."""

    symbol: str | None                 # resolved broker symbol, None if unresolved
    periods: tuple[int, int, int]      # (fast, mid, center) e.g. (20, 80, 320)
    price: float | None                # last confirmed close
    ema_fast: float | None             # EMA20 (latest confirmed)
    ema_mid: float | None              # EMA80
    ema_center: float | None           # EMA320 (the centerline)
    times_ms: tuple[int, ...]          # epoch ms per displayed bar (chronological)
    dev_price: tuple[float, ...]       # (close   − EMA320) / EMA320 * 100
    dev_fast: tuple[float, ...]        # (EMA20   − EMA320) / EMA320 * 100
    dev_mid: tuple[float, ...]         # (EMA80   − EMA320) / EMA320 * 100
    as_of: float                       # epoch seconds
    stale: bool                        # True when no live data
    bands: dict | None = None          # disparity percentile bands (feature 1)
    mode: str = "M15"                  # oscillator mode name (M15 / H1)


def _stale_snapshot(symbol: str | None, *,
                    periods: tuple[int, int, int] = config.EMA_STACK_PERIODS,
                    mode: str = "M15") -> EmaStackSnapshot:
    """A fully-empty snapshot (no live data), optionally tagged with the symbol."""
    return EmaStackSnapshot(
        symbol=symbol, periods=periods,
        price=None, ema_fast=None, ema_mid=None, ema_center=None,
        times_ms=(), dev_price=(), dev_fast=(), dev_mid=(),
        as_of=time.time(), stale=True, bands=load_bands(mode), mode=mode,
    )


def _ema(values: np.ndarray, period: int) -> np.ndarray:
    """Causal EMA seeded with the first value (``adjust=False``): identical
    recursion to the rest of the codebase (``y[0]=x[0]``)."""
    return pd.Series(values).ewm(span=period, adjust=False).mean().to_numpy()


def _tf_const(tf_label: str) -> int:
    """mt5 timeframe constant for *tf_label* (falls back to M15)."""
    spec = config.TIMEFRAME_BY_LABEL.get(tf_label)
    return spec.mt5_const if spec is not None else mt5.TIMEFRAME_M15


def compute_ema_stack(
    connector,
    *,
    tf: str = config.EMA_STACK_TF,
    periods: tuple[int, int, int] = config.EMA_STACK_PERIODS,
    fetch_bars: int = config.EMA_STACK_FETCH_BARS,
    display_bars: int = config.EMA_STACK_DISPLAY_BARS,
    mode: str = "M15",
) -> EmaStackSnapshot:
    """Compute the EMA-stack oscillator for XAUUSD from a deep *tf* history.

    Returns a STALE empty snapshot when the symbol is unresolved, the broker
    returns no bars, or there is too little history for the center EMA. Otherwise
    fetches *fetch_bars* bars of *tf*, drops the forming last bar (closed bars
    only), computes the three *periods* EMAs and their %-deviation from the
    center EMA, and emits the trailing *display_bars*. Defaults are the M15 mode;
    ``compute_ema_stack_for`` drives the H1 mode. Never raises into the loop.
    """
    sym = config.SYMBOLS[0].base
    if sym not in connector.resolved_symbols:
        return _stale_snapshot(None, periods=periods, mode=mode)

    try:
        df = connector.copy_rates(sym, _tf_const(tf), fetch_bars)
    except Exception:  # noqa: BLE001 — feed must never raise into the loop
        log.exception("compute_ema_stack: copy_rates failed for %s", sym)
        return _stale_snapshot(sym, periods=periods, mode=mode)

    if df is None or df.empty or "close" not in df.columns:
        return _stale_snapshot(sym, periods=periods, mode=mode)

    # Closed bars only — drop the still-forming last bar (codebase convention),
    # so every emitted bar's EMAs are final and never repaint.
    closed = df.iloc[:-1]
    closes = closed["close"].to_numpy(dtype=float)
    p_fast, p_mid, p_center = periods
    # Need enough history for the center EMA to be meaningful (its warm-up must
    # have decayed well before the displayed window).
    if closes.size < p_center:
        return _stale_snapshot(sym, periods=periods, mode=mode)

    ema_f = _ema(closes, p_fast)
    ema_m = _ema(closes, p_mid)
    ema_c = _ema(closes, p_center)
    times_full = closed.index.values.astype("datetime64[ns]").astype("int64") // 1_000_000

    n = display_bars
    sl = slice(-n, None)
    c = ema_c[sl]

    def _dev(arr: np.ndarray) -> tuple[float, ...]:
        return tuple(
            float((x - y) / y * 100.0) if y else 0.0
            for x, y in zip(arr[sl], c)
        )

    return EmaStackSnapshot(
        symbol=sym,
        periods=periods,
        price=float(closes[-1]),
        ema_fast=float(ema_f[-1]),
        ema_mid=float(ema_m[-1]),
        ema_center=float(ema_c[-1]),
        times_ms=tuple(int(t) for t in times_full[sl]),
        dev_price=_dev(closes),
        dev_fast=_dev(ema_f),
        dev_mid=_dev(ema_m),
        as_of=time.time(),
        stale=False,
        bands=load_bands(mode),
        mode=mode,
    )


def compute_ema_stack_for(connector, mode_spec, *, deep: bool = False) -> EmaStackSnapshot:
    """Compute one oscillator mode from a ``config.EmaStackMode`` spec.

    ``deep=True`` uses the larger history fetch/display sizes (for the
    /api/ema_history endpoint); otherwise the small live-snapshot sizes.
    """
    fb = mode_spec.history_fetch_bars if deep else mode_spec.fetch_bars
    db = mode_spec.history_bars if deep else mode_spec.display_bars
    return compute_ema_stack(
        connector, tf=mode_spec.tf, periods=mode_spec.periods,
        fetch_bars=fb, display_bars=db, mode=mode_spec.name)
