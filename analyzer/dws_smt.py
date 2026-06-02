"""DWS-SMT multi-timeframe trend indicator — port of DWS_SMT.mq5 v2.00.

The MQL5 indicator stacks three timeframe rows (its ``TF1/TF2/TF3`` inputs).
For each row it computes ``diff = close − EMA(SMT_Period)``, maps that diff
onto the chart's own bars, smooths the mapped series with ``EMA(Smooth)`` and
colours each bar by the smoothed sign (green up / red down / grey flat). When
all three rows share the same colour on a *confirmed* bar the indicator emits
a BUY (all green) or SELL (all red) trigger; when an existing alignment breaks
it emits EXIT.

This module reproduces that pipeline for the dashboard. The selected *base*
timeframe anchors its own 3-TF stack — the base TF is the bottom row and the
two next-higher timeframes stack above it (M15 → H4/H1/M15, H1 → D1/H4/H1,
H4 → W1/D1/H4) — and is also the histogram's x-axis resolution. Switching the
base therefore slides the whole stack up/down the timeframe ladder
(``config.DWS_SMT_STACKS``).

Faithfulness notes
------------------
* The diff EMA is seeded with the first close (``emaArr[0] = tfClose[0]`` in the
  ``.mq5``), reproduced here with an ``lfilter`` initial state of
  ``(1−α)·close[0]``.
* The diff-smoothing EMA is seeded with 0 (the ``.mq5`` ``static double sm = 0``),
  so the first smoothed value is ``α · diff[0]``.
* Triggers fire only on *confirmed* bars: never on the oldest bar (no prior
  state) and never on the rightmost in-progress bar — exactly the ``.mq5``
  ``bar >= 1 && i > 0`` guard.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.signal import lfilter

import config

log = logging.getLogger(__name__)

# Colour indices — order matches DWS_SMT.mq5 (clrLime, clrRed, clrGray).
COLOR_UP: int = 0
COLOR_DOWN: int = 1
COLOR_NEUTRAL: int = 2


# --------------------------------------------------------------------------- #
# Result dataclasses
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class DwsSmtTrade:
    """One DWS-SMT trade: a BUY/SELL entry paired with its closing trigger.

    ``points`` is the signed close-to-close price move in the trade's favour
    (positive = profit). For the still-open trade it is the floating P/L marked
    to the most recent close. Spread/slippage are not deducted. ``mae`` is the
    Maximum Adverse Excursion — the worst intra-trade drawdown in price units
    (>= 0; 0 means the trade was never underwater).
    """

    entry_idx: int        # bar index (within the emitted window) of the entry
    direction: int        # +1 long / -1 short
    points: float         # signed price points (realised, or floating if open)
    mae: float            # max adverse excursion in price units (>= 0)
    is_open: bool         # True = position still open, points is floating P/L


@dataclass(frozen=True)
class DwsSmtWindow:
    """The DWS-SMT histogram rendered against one base timeframe.

    Stored column-wise as parallel arrays (one entry per base bar) rather than
    as per-bar objects: building ~100 row objects per (symbol, base TF) every
    analysis cycle is far too slow for the SPEC §19 budget. The serializer
    fans these arrays out into the per-bar JSON the front end consumes.
    """

    base_tf: str                       # "M15" / "H1" / "H4"
    rows: tuple[str, ...]              # row timeframes top→bottom for this base
    times_ms: np.ndarray               # int64 epoch milliseconds, one per bar
    colors: np.ndarray                 # int8 (n_bars, n_rows) — colour per row
    triggers: tuple[str | None, ...]   # "BUY"/"SELL"/"EXIT"/None, one per bar
    trades: tuple[DwsSmtTrade, ...]    # paired entry→exit trades over the window
    bias: np.ndarray                   # composite BIAS score (-10..+10) per bar
    flip_norm: np.ndarray              # signed (n_bars, n_rows) distance-to-flip


@dataclass(frozen=True)
class DwsSmtResult:
    """DWS-SMT output for one symbol across every base timeframe."""

    by_base: dict[str, DwsSmtWindow]       # keyed by base timeframe label


# --------------------------------------------------------------------------- #
# Low-level helpers
# --------------------------------------------------------------------------- #

def _epoch_ns(index: pd.Index) -> np.ndarray:
    """Return a DatetimeIndex as int64 nanoseconds since the epoch (UTC)."""
    return index.values.astype("datetime64[ns]").astype("int64")


def _ema(values: np.ndarray, alpha: float, seed: float) -> np.ndarray:
    """Recursive EMA ``y[k] = α·x[k] + (1−α)·y[k−1]`` with ``y[−1] = seed``.

    Delegated to ``scipy.signal.lfilter`` (one C call) — *seed* is the
    conceptual value before the series. Passing ``seed = x[0]`` reproduces the
    ``.mq5`` first-value seeding (``y[0] = x[0]``); ``seed = 0`` reproduces the
    ``.mq5`` smoothing seed (``y[0] = α·x[0]``).
    """
    x = np.asarray(values, dtype=np.float64)
    if x.size == 0:
        return x
    one_minus_alpha = 1.0 - alpha
    b = [alpha]
    a = [1.0, -one_minus_alpha]
    zi = [one_minus_alpha * seed]
    y, _ = lfilter(b, a, x, zi=zi)
    return y


def _diff_series(df: pd.DataFrame, period: int) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(epoch_ns, diff)`` where ``diff = close − EMA(period)``."""
    close = df["close"].to_numpy(dtype=np.float64)
    ema = _ema(close, alpha=2.0 / (period + 1.0), seed=float(close[0]))
    return _epoch_ns(df.index), close - ema


def _map_onto(
    base_ns: np.ndarray, sub_ns: np.ndarray, sub_diff: np.ndarray
) -> np.ndarray:
    """Map a sub-TF diff series onto base bars as a step function.

    Each base bar takes the diff of the most recent sub-TF bar whose time is
    ``<=`` the base bar time; base bars older than the first sub bar take 0.
    Mirrors the ``.mq5`` ``CalcTfDiffArray`` tfIdx sweep.
    """
    idx = np.searchsorted(sub_ns, base_ns, side="right") - 1
    return np.where(idx >= 0, sub_diff[np.clip(idx, 0, None)], 0.0)


def _colorize(smoothed: np.ndarray) -> np.ndarray:
    """Map smoothed values to colour indices (the ``.mq5`` ``SmoothAndColor``)."""
    out = np.full(smoothed.size, COLOR_NEUTRAL, dtype=np.int8)
    out[smoothed > 0.0] = COLOR_UP
    out[smoothed < 0.0] = COLOR_DOWN
    return out


def _flip_norm(smoothed: np.ndarray, window: int, k: float) -> np.ndarray:
    """Signed, self-normalised distance of the smoothed diff from its zero-cross.

    ``flip_norm = clamp(smoothed / (k * rolling_std(smoothed, window)), -1, +1)``
    where ``rolling_std`` is the trailing population std of the row's own
    smoothed-diff series. The SIGN is the row's current direction; the MAGNITUDE
    is how firmly aligned it is (``~0`` = at the zero-cross = a colour flip / a
    trigger is imminent; ``~1`` = firmly in its colour). Returns 0 wherever the
    scale is undefined (warmup with <2 points, or a flat zero-variance window),
    never NaN/inf — mirroring ``_colorize`` treating a flat series as neutral.

    The trailing population std uses a VECTORISED cumulative-sum rolling window,
    not ``pandas.rolling``: this runs ~72 times per analysis cycle (row x base
    stack x symbol) and pandas' fixed per-call overhead (~0.3 ms) summed to
    ~20 ms, pushing the SPEC 19 analysis budget over 50 ms. The cumsum form is
    O(n), allocation-light, and matches the population std (ddof=0)."""
    n = smoothed.size
    out = np.zeros(n, dtype=np.float64)
    if n == 0:
        return out
    # Windowed sum over (lo..i] = c[i+1] - c[lo] via prefix sums.
    c1 = np.concatenate(([0.0], np.cumsum(smoothed)))
    c2 = np.concatenate(([0.0], np.cumsum(smoothed * smoothed)))
    idx = np.arange(n)
    lo = np.maximum(0, idx - window + 1)
    cnt = (idx - lo + 1).astype(np.float64)
    s = c1[idx + 1] - c1[lo]
    ss = c2[idx + 1] - c2[lo]
    mean = s / cnt
    var = np.maximum(ss / cnt - mean * mean, 0.0)     # clamp tiny negatives
    denom = k * np.sqrt(var)
    ok = (cnt >= 2.0) & (denom > 0.0)                 # <2 pts or flat -> leave 0
    out[ok] = np.clip(smoothed[ok] / denom[ok], -1.0, 1.0)
    return out


def _detect_triggers(colors_by_row: list[np.ndarray]) -> list[str | None]:
    """Edge-detect BUY/SELL/EXIT over the base bars (the ``.mq5`` OnCalculate loop).

    State is +1 when every row is green, −1 when every row is red, else 0. A
    trigger fires when the state differs from the previous bar's — but only on
    *confirmed* bars: never the oldest bar (no prior state) and never the
    last/in-progress bar (the ``.mq5`` ``bar >= 1 && i > 0`` guard).

    Every bar except the in-progress one is confirmed, so the previous *bar*
    is always the previous *confirmed* state — which makes the whole edge scan
    a vectorised ``state[1:n-1]`` vs ``state[:n-2]`` comparison.
    """
    colors = np.stack(colors_by_row)                       # (n_rows, n)
    n = colors.shape[1]
    triggers: list[str | None] = [None] * n
    all_up = (colors == COLOR_UP).all(axis=0)
    all_down = (colors == COLOR_DOWN).all(axis=0)
    state = np.where(all_up, 1, np.where(all_down, -1, 0))
    cur = state[1:n - 1]                                    # confirmed bars 1..n-2
    prev = state[0:n - 2]                                   # their predecessors
    for j in np.flatnonzero((cur == 1) & (prev != 1)):
        triggers[int(j) + 1] = "BUY"
    for j in np.flatnonzero((cur == -1) & (prev != -1)):
        triggers[int(j) + 1] = "SELL"
    for j in np.flatnonzero((cur == 0) & (prev != 0)):
        triggers[int(j) + 1] = "EXIT"
    return triggers


def _trade_mae(
    direction: int, entry_price: float,
    highs: np.ndarray, lows: np.ndarray, entry_idx: int, exit_idx: int,
) -> float:
    """Max Adverse Excursion: the worst intra-trade move against the position,
    in price units (>= 0; 0 means the trade was never underwater)."""
    if exit_idx < entry_idx:
        return 0.0
    if direction == 1:                                       # long → worst low
        worst = float(lows[entry_idx:exit_idx + 1].min())
        return max(0.0, entry_price - worst)
    worst = float(highs[entry_idx:exit_idx + 1].max())        # short → worst high
    return max(0.0, worst - entry_price)


def _pair_trades(
    triggers: tuple[str | None, ...],
    closes: np.ndarray, highs: np.ndarray, lows: np.ndarray,
) -> tuple[DwsSmtTrade, ...]:
    """Pair BUY/SELL entry triggers with their closing trigger into trades.

    Model: BUY opens a long, SELL a short (entry = that bar's close). A trade
    closes on the next EXIT (flat) or opposite signal (stop-and-reverse). The
    last unclosed entry becomes an open trade with floating P/L marked to the
    most recent close. An EXIT with no open position (a trade entered before
    the window) is an orphan and ignored. Each trade also carries its MAE.
    """
    trades: list[DwsSmtTrade] = []
    pos_dir = 0
    pos_entry_idx = -1
    pos_entry_price = 0.0
    n = len(triggers)

    def _close(exit_idx: int, exit_price: float, is_open: bool) -> None:
        trades.append(DwsSmtTrade(
            entry_idx=pos_entry_idx, direction=pos_dir,
            points=(exit_price - pos_entry_price) * pos_dir,
            mae=_trade_mae(pos_dir, pos_entry_price, highs, lows,
                           pos_entry_idx, exit_idx),
            is_open=is_open))

    for j, g in enumerate(triggers):
        if g is None:
            continue
        price = float(closes[j])
        if g in ("BUY", "SELL"):
            new_dir = 1 if g == "BUY" else -1
            if pos_dir not in (0, new_dir):                  # reversal — close first
                _close(j, price, is_open=False)
            if pos_dir != new_dir:
                pos_dir, pos_entry_idx, pos_entry_price = new_dir, j, price
        elif g == "EXIT" and pos_dir != 0:
            _close(j, price, is_open=False)
            pos_dir = 0
    if pos_dir != 0:
        _close(n - 1, float(closes[n - 1]), is_open=True)
    return tuple(trades)


# --------------------------------------------------------------------------- #
# Window builder + public entry point
# --------------------------------------------------------------------------- #

def _bias_series(
    base_ns: np.ndarray,
    bias_contrib: dict[str, tuple[np.ndarray, np.ndarray]] | None,
) -> np.ndarray:
    """Composite BIAS score (-10..+10) per base bar.

    Each BIAS timeframe's regime-gated contribution series is mapped onto the
    base bars (step function, the value as of that bar's time) and weighted-
    summed — so the score is the dashboard BIAS *as it was at each bar*, free
    of look-ahead. Returns all-zeros when no contribution data is supplied.
    """
    n = base_ns.size
    score = np.zeros(n, dtype=np.float64)
    if not bias_contrib:
        return score
    total_w = 0.0
    for tf, w in config.BIAS_TF_WEIGHTS.items():
        bc = bias_contrib.get(tf)
        if bc is None:
            continue
        sub_ns, contrib = bc
        score += _map_onto(base_ns, sub_ns, contrib) * w
        total_w += w
    if total_w <= 0:
        return np.zeros(n, dtype=np.float64)
    return score / (2.0 * total_w) * 10.0


def _build_window(
    base: str,
    base_df: pd.DataFrame,
    rows: tuple[str, ...],
    row_diffs: dict[str, tuple[np.ndarray, np.ndarray]],
    smooth: int,
    out_bars: int,
    bias_contrib: dict[str, tuple[np.ndarray, np.ndarray]] | None,
    flip_window: int,
    flip_k: float,
) -> DwsSmtWindow | None:
    """Render the DWS-SMT histogram for one base timeframe."""
    base_ns = _epoch_ns(base_df.index)
    n = base_ns.size
    if n < 2:
        return None

    salpha = 2.0 / (smooth + 1.0)
    # Colour each row over the *full* base history so the zero-seeded smoothing
    # warm-up has fully decayed before the trailing window that gets emitted.
    # The smoothed magnitude (whose SIGN _colorize keeps) is REUSED for the
    # flip-proximity gradient — no second pass — so the gradient is essentially
    # free. Both the colour and the proximity therefore share the same
    # forming-EXCLUDED, look-ahead-safe series.
    colors_by_row: list[np.ndarray] = []
    flip_by_row: list[np.ndarray] = []
    for label in rows:
        rd = row_diffs.get(label)
        mapped = (np.zeros(n, dtype=np.float64) if rd is None
                  else _map_onto(base_ns, rd[0], rd[1]))
        smoothed = _ema(mapped, salpha, seed=0.0)
        colors_by_row.append(_colorize(smoothed))
        flip_by_row.append(_flip_norm(smoothed, flip_window, flip_k))

    triggers = _detect_triggers(colors_by_row)

    # Emit the trailing window as plain arrays — pure numpy slicing, no
    # per-bar Python objects (those are built later by the serializer, off
    # the SPEC §19 compute budget).
    bias = _bias_series(base_ns, bias_contrib)

    start = max(0, n - out_bars)
    colors = np.stack([row[start:] for row in colors_by_row], axis=1)   # (n_out, n_rows)
    flip = np.stack([row[start:] for row in flip_by_row], axis=1)       # (n_out, n_rows)
    times_ms = base_ns[start:] // 1_000_000                             # ns → ms
    out_triggers = tuple(triggers[start:])
    out_closes = base_df["close"].to_numpy(dtype=np.float64)[start:]
    out_highs = base_df["high"].to_numpy(dtype=np.float64)[start:]
    out_lows = base_df["low"].to_numpy(dtype=np.float64)[start:]
    return DwsSmtWindow(
        base_tf=base,
        rows=tuple(rows),
        times_ms=times_ms,
        colors=colors,
        triggers=out_triggers,
        trades=_pair_trades(out_triggers, out_closes, out_highs, out_lows),
        bias=bias[start:],
        flip_norm=flip,
    )


def compute_symbol(
    frames: dict[str, pd.DataFrame],
    *,
    stacks: dict[str, tuple[str, ...]] = config.DWS_SMT_STACKS,
    period: int = config.DWS_SMT_PERIOD,
    smooth: int = config.DWS_SMT_SMOOTH,
    out_bars: int = config.DWS_SMT_BARS,
    bias_contrib: dict[str, tuple[np.ndarray, np.ndarray]] | None = None,
    flip_window: int = config.DWS_FLIP_STD_WINDOW,
    flip_k: float = config.DWS_FLIP_K,
) -> DwsSmtResult | None:
    """Compute the DWS-SMT histogram for one symbol across every base timeframe.

    Args:
        frames: ``{tf_label: OHLC DataFrame}`` covering the base timeframes and
            every row timeframe referenced by *stacks*. Each DataFrame is
            time-indexed (ascending) and carries a ``close`` column.
        stacks: ``{base_tf: (row_tf, ...)}`` — each base timeframe's own
            top→bottom row stack (the base TF is the bottom row).
        period: EMA period for the ``close − EMA`` diff (``.mq5`` SMT_Period).
        smooth: EMA period for smoothing the mapped diff (``.mq5`` Smooth).
        out_bars: how many trailing base bars to emit per base timeframe.
        bias_contrib: ``{tf_label: (times_ns, contribution)}`` per-bar
            regime-gated BIAS contribution series, used to build the per-bar
            historical BIAS score. When ``None`` the BIAS series is all zeros.

    Returns:
        A :class:`DwsSmtResult`, or ``None`` when no base timeframe could be
        built (every base DataFrame missing or too short).
    """
    # A row TF's diff is identical wherever it appears, so compute the diff for
    # each distinct row TF across all stacks exactly once and reuse it.
    #
    # NO LOOK-AHEAD: the last bar of each row frame is the in-progress (forming)
    # candle whose close is still drifting tick-by-tick. Feeding it into
    # ``_diff_series`` lets the forming sub-TF colour propagate through
    # ``_map_onto`` onto every confirmed base bar whose timestamp falls inside
    # the still-open sub-TF candle — a textbook look-ahead leak that makes
    # confirmed-bar triggers flicker as the forming bar moves. ``iloc[:-1]``
    # drops it so confirmed base bars only see closed sub-TF diffs. The base
    # frame itself is NOT trimmed below: its forming bar is excluded from
    # trigger detection by ``_detect_triggers`` (``state[1:n-1]``) but its
    # latest close is still needed for the open trade's mark-to-market.
    #
    # The flip-proximity gradient reuses this same forming-EXCLUDED smoothed
    # series (inside _build_window) — no separate forming-inclusive pass — so the
    # gradient is look-ahead-safe and essentially free.
    needed = {tf for stack in stacks.values() for tf in stack}
    row_diffs: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for label in needed:
        df = frames.get(label)
        if df is not None and not df.empty and len(df) > 2:
            row_diffs[label] = _diff_series(df.iloc[:-1], period)

    by_base: dict[str, DwsSmtWindow] = {}
    for base, rows in stacks.items():
        base_df = frames.get(base)
        if base_df is None or base_df.empty or len(base_df) < 2:
            continue
        window = _build_window(base, base_df, rows, row_diffs,
                               smooth, out_bars, bias_contrib,
                               flip_window, flip_k)
        if window is not None:
            by_base[base] = window

    if not by_base:
        return None
    return DwsSmtResult(by_base=by_base)
