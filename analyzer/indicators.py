"""Vectorised technical indicators for the dashboard.

SPEC 6 enumerates exactly the indicators we need and pins their canonical
formulas (Wilder smoothing for ATR/RSI/ADX, EMA on Close for moving averages).
Each function returns a ``numpy.ndarray`` of the same length as the input
``close`` series; warmup values that cannot be computed yet are filled with
``NaN`` so callers can pick the latest valid value with ``np.isfinite``.

Performance
-----------
The recursive parts (EMA, Wilder RMA) are delegated to ``pandas.Series.ewm``
which dispatches to a C implementation. A pure-Python loop made the
10-symbol × 4-TF pass land ~88 ms, exceeding SPEC §19's 50 ms budget; the
vectorised version measures well below 20 ms on the same data.

The seed semantics match Wilder / MT5 / TradingView exactly:

* EMA: first valid value = SMA of the first ``period`` values; thereafter
  ``y[i] = alpha*x[i] + (1-alpha)*y[i-1]`` with ``alpha = 2/(period+1)``.
* Wilder RMA: first valid value = SMA of the first ``period`` values;
  thereafter ``y[i] = (y[i-1]*(period-1) + x[i]) / period``, i.e. an EMA
  with ``alpha = 1/period``.

References:
    Wilder, J. Welles (1978), *New Concepts in Technical Trading Systems*.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray
from scipy.signal import lfilter

ArrayF = NDArray[np.float64]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _as_f64(x: NDArray) -> ArrayF:
    return np.asarray(x, dtype=np.float64)


def _recursive_ema(values: NDArray, period: int, alpha: float) -> ArrayF:
    """SMA-seeded recursive EMA via ``scipy.signal.lfilter``.

    Accepts a 1-D series or a 2-D matrix of stacked series along axis 0
    (i.e. shape ``(n_series, n_samples)``). When 2-D, the per-series SMA
    seed is computed per row and lfilter is invoked with ``axis=1`` so a
    single C-level call handles every series. This is the speed lever
    that lets the 10-symbol × 4-TF pass land under the SPEC §19 50 ms
    budget at the cost of one extra reshape per indicator.

    Args:
        values: input series, shape ``(n_samples,)`` or ``(n_series, n_samples)``.
        period: SMA-seed window length (also the NaN warmup zone).
        alpha: smoothing factor. EMA: ``2/(period+1)``. Wilder: ``1/period``.

    Returns:
        Array of the same shape as the input; positions before
        ``period - 1`` along the last axis are NaN.
    """
    v = np.asarray(values, dtype=np.float64)
    if v.ndim == 1:
        v2 = v[np.newaxis, :]
        squeeze = True
    elif v.ndim == 2:
        v2 = v
        squeeze = False
    else:
        raise ValueError(f"expected 1-D or 2-D input, got ndim={v.ndim}")

    n_series, n = v2.shape
    out = np.full(v2.shape, np.nan, dtype=np.float64)
    if n < period:
        return out[0] if squeeze else out

    seed = v2[:, :period].mean(axis=1)              # (n_series,)
    out[:, period - 1] = seed
    if n == period:
        return out[0] if squeeze else out

    one_minus_alpha = 1.0 - alpha
    b = np.array([alpha], dtype=np.float64)
    a = np.array([1.0, -one_minus_alpha], dtype=np.float64)
    # lfilter zi for first-order: shape (..., 1).
    zi = (one_minus_alpha * seed)[:, np.newaxis]    # (n_series, 1)
    tail, _ = lfilter(b, a, v2[:, period:], axis=1, zi=zi)
    out[:, period:] = tail
    return out[0] if squeeze else out


def _wilder_smoothing(values: NDArray, period: int) -> ArrayF:
    """Wilder RMA: ``alpha = 1/period``, SMA seed. Accepts 1-D or 2-D input."""
    return _recursive_ema(values, period, alpha=1.0 / period)


# --------------------------------------------------------------------------- #
# EMA (SPEC 6.1)
# --------------------------------------------------------------------------- #

def ema(close: NDArray, period: int) -> ArrayF:
    """Exponential moving average on Close (SMA seed, ``alpha = 2/(p+1)``).

    Accepts 1-D ``(n_samples,)`` or 2-D ``(n_series, n_samples)`` input.
    """
    if period <= 0:
        raise ValueError(f"period must be > 0, got {period}")
    return _recursive_ema(close, period, alpha=2.0 / (period + 1.0))


# --------------------------------------------------------------------------- #
# ATR (SPEC 6.4) — Wilder
# --------------------------------------------------------------------------- #

def true_range(high: NDArray, low: NDArray, close: NDArray) -> ArrayF:
    """True Range = max(H-L, |H-prevC|, |L-prevC|). First bar = H-L.

    Accepts 1-D or stacked 2-D ``(n_series, n_samples)`` arrays.
    """
    h, l, c = _as_f64(high), _as_f64(low), _as_f64(close)
    if not (h.shape == l.shape == c.shape):
        raise ValueError("high/low/close arrays must be the same shape")
    if h.ndim not in (1, 2):
        raise ValueError(f"true_range expects 1-D or 2-D arrays, got ndim={h.ndim}")

    tr = np.empty_like(h)
    if h.ndim == 1:
        tr[0] = h[0] - l[0]
        prev_close = c[:-1]
        tr[1:] = np.maximum.reduce([
            h[1:] - l[1:],
            np.abs(h[1:] - prev_close),
            np.abs(l[1:] - prev_close),
        ])
    else:
        tr[:, 0] = h[:, 0] - l[:, 0]
        prev_close = c[:, :-1]
        tr[:, 1:] = np.maximum.reduce([
            h[:, 1:] - l[:, 1:],
            np.abs(h[:, 1:] - prev_close),
            np.abs(l[:, 1:] - prev_close),
        ])
    return tr


def atr(high: NDArray, low: NDArray, close: NDArray, period: int = 14) -> ArrayF:
    """Wilder ATR(period). First *period-1* values are NaN. 1-D or 2-D input."""
    tr = true_range(high, low, close)
    return _wilder_smoothing(tr, period)


# --------------------------------------------------------------------------- #
# RSI (SPEC 6.3) — Wilder
# --------------------------------------------------------------------------- #

def rsi(close: NDArray, period: int = 14) -> ArrayF:
    """Wilder RSI. First *period* values are NaN. 1-D or stacked 2-D input."""
    c = _as_f64(close)
    if c.ndim not in (1, 2):
        raise ValueError(f"rsi expects 1-D or 2-D, got ndim={c.ndim}")

    n = c.shape[-1]
    out_shape = c.shape
    out = np.full(out_shape, np.nan, dtype=np.float64)
    if n <= period:
        return out

    # Compute gains/losses along the last axis.
    diff_axis = -1
    diffs = np.diff(c, axis=diff_axis)            # shape (..., n-1)
    gains = np.where(diffs > 0, diffs, 0.0)
    losses = np.where(diffs < 0, -diffs, 0.0)

    avg_gain_arr = _wilder_smoothing(gains, period)   # shape (..., n-1)
    avg_loss_arr = _wilder_smoothing(losses, period)

    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain_arr / avg_loss_arr
        rsi_arr = 100.0 - (100.0 / (1.0 + rs))
        # avg_loss == 0 ⇒ RSI = 100 by convention (when gain is finite).
        zero_loss = (avg_loss_arr == 0) & np.isfinite(avg_gain_arr)
        rsi_arr = np.where(zero_loss, 100.0, rsi_arr)

    # Place into output array offset by +1 along the last axis.
    if c.ndim == 1:
        out[1:] = rsi_arr
    else:
        out[:, 1:] = rsi_arr
    return out


# --------------------------------------------------------------------------- #
# ADX / DI+ / DI- (SPEC 6.2) — Wilder
# --------------------------------------------------------------------------- #

def adx(
    high: NDArray,
    low: NDArray,
    close: NDArray,
    period: int = 14,
) -> tuple[ArrayF, ArrayF, ArrayF]:
    """Return (adx, di_plus, di_minus). All NaN for the warmup window.

    Accepts 1-D ``(n,)`` or stacked 2-D ``(n_series, n)`` arrays.

    Implementation follows Wilder exactly:

    * ``up   = high[i] - high[i-1]``
    * ``down = low[i-1] - low[i]``
    * ``+DM = up    if up > down and up > 0    else 0``
    * ``-DM = down  if down > up and down > 0  else 0``
    * smooth +DM / -DM / TR with Wilder RMA over *period*
    * +DI = 100 * smooth(+DM) / smooth(TR)
    * -DI = 100 * smooth(-DM) / smooth(TR)
    * DX  = 100 * |+DI - -DI| / (+DI + -DI)
    * ADX = Wilder RMA of DX over *period*
    """
    h, l, c = _as_f64(high), _as_f64(low), _as_f64(close)
    if not (h.shape == l.shape == c.shape):
        raise ValueError("high/low/close arrays must be the same shape")
    if h.ndim not in (1, 2):
        raise ValueError(f"adx expects 1-D or 2-D arrays, got ndim={h.ndim}")

    n = h.shape[-1]
    if n < 2 * period + 1:
        nan_arr = np.full(h.shape, np.nan, dtype=np.float64)
        return nan_arr.copy(), nan_arr.copy(), nan_arr.copy()

    # Bar 0 has no prior bar, so its +DM/-DM are undefined. Slice from bar 1
    # so the Wilder seed (mean of the first `period` post-slice values) uses
    # bars 1..period exactly as Wilder / MT5 iADX do. The output is then
    # padded with one NaN at the front to preserve alignment.
    last_axis = -1
    if h.ndim == 1:
        h1, l1, c1 = h[1:], l[1:], c[1:]
        prev_h, prev_l = h[:-1], l[:-1]
    else:
        h1, l1, c1 = h[:, 1:], l[:, 1:], c[:, 1:]
        prev_h, prev_l = h[:, :-1], l[:, :-1]

    up = h1 - prev_h
    down = prev_l - l1
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr1 = true_range(h, l, c)
    tr_inner = tr1[1:] if h.ndim == 1 else tr1[:, 1:]

    smooth_plus = _wilder_smoothing(plus_dm, period)
    smooth_minus = _wilder_smoothing(minus_dm, period)
    smooth_tr = _wilder_smoothing(tr_inner, period)

    with np.errstate(divide="ignore", invalid="ignore"):
        di_plus_inner = 100.0 * smooth_plus / smooth_tr
        di_minus_inner = 100.0 * smooth_minus / smooth_tr
        dx_denom = di_plus_inner + di_minus_inner
        dx = np.where(
            dx_denom > 0,
            100.0 * np.abs(di_plus_inner - di_minus_inner) / dx_denom,
            0.0,
        )

    adx_inner = _wilder_smoothing(dx, period)

    # Pad one NaN column at the front to realign with the original n-bar axis.
    def _pad_front(arr: ArrayF) -> ArrayF:
        if arr.ndim == 1:
            return np.concatenate(([np.nan], arr))
        nan_col = np.full((arr.shape[0], 1), np.nan, dtype=np.float64)
        return np.concatenate([nan_col, arr], axis=last_axis)

    adx_out = _pad_front(adx_inner)
    di_plus = np.where(np.isnan(_pad_front(smooth_tr)), np.nan, _pad_front(di_plus_inner))
    di_minus = np.where(np.isnan(_pad_front(smooth_tr)), np.nan, _pad_front(di_minus_inner))
    return adx_out, di_plus, di_minus


# --------------------------------------------------------------------------- #
# Convenience: latest finite value
# --------------------------------------------------------------------------- #

def last_finite(arr: NDArray) -> float | None:
    """Return the last non-NaN value as float, or None if all NaN."""
    a = np.asarray(arr, dtype=np.float64)
    if a.size == 0:
        return None
    mask = np.isfinite(a)
    if not mask.any():
        return None
    idx = np.flatnonzero(mask)[-1]
    return float(a[idx])
