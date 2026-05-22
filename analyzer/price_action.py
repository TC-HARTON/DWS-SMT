"""M15 price-action patterns (SPEC §11).

SPEC §11.1 restricts price-action detection to M15 only — every helper
here expects a single M15 DataFrame. The output dataclasses are JSON
friendly and consumed by the dashboard's symbol panel.

Definitions are taken verbatim from SPEC §11.2 so the rules survive a
spec re-read:

* Pin (bull):  下ヒゲ ≥ 実体 × 2  AND  上ヒゲ < 実体 × 0.3
* Pin (bear):  上ヒゲ ≥ 実体 × 2  AND  下ヒゲ < 実体 × 0.3
* Engulfing bull: 当該足の陽線実体が前足の陰線実体を完全包含
* Engulfing bear: 当該足の陰線実体が前足の陽線実体を完全包含
* Inside bar: 当該足の高安が前足範囲内
* Inside-bar break: インサイドバー直後のN本目で方向ブレイク
* 3-bar reversal: 3本連続の反転構造 (down,down,up that closes above prior high
                                   for bull; up,up,down that closes below prior low
                                   for bear)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import numpy as np
import pandas as pd

import config


class PatternKind(str, Enum):
    PIN_BULL = "pin_bull"
    PIN_BEAR = "pin_bear"
    ENGULF_BULL = "engulf_bull"
    ENGULF_BEAR = "engulf_bear"
    INSIDE = "inside"
    INSIDE_BREAK_UP = "inside_break_up"
    INSIDE_BREAK_DOWN = "inside_break_down"
    THREE_BAR_REVERSAL_UP = "three_bar_up"
    THREE_BAR_REVERSAL_DOWN = "three_bar_down"


@dataclass(frozen=True)
class PriceActionEvent:
    """One detected pattern on a closed bar."""

    kind: PatternKind
    bar_time: pd.Timestamp        # tz-aware UTC; the bar that "owns" the pattern
    bar_index_from_end: int       # 0 = latest bar, 1 = previous, ...
    direction: int                # +1 bullish, -1 bearish, 0 neutral
    close: float
    extreme: float                # bar high (bear) or bar low (bull)
    body: float                   # |open - close|
    note: str = ""
    meta: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Bar geometry primitives
# --------------------------------------------------------------------------- #

def _geometry(df: pd.DataFrame) -> dict[str, np.ndarray]:
    o = df["open"].to_numpy(dtype=np.float64)
    h = df["high"].to_numpy(dtype=np.float64)
    l = df["low"].to_numpy(dtype=np.float64)
    c = df["close"].to_numpy(dtype=np.float64)
    body = np.abs(c - o)
    upper_wick = h - np.maximum(o, c)
    lower_wick = np.minimum(o, c) - l
    return {
        "o": o, "h": h, "l": l, "c": c,
        "body": body, "upper": upper_wick, "lower": lower_wick,
    }


# --------------------------------------------------------------------------- #
# Per-pattern detectors
# --------------------------------------------------------------------------- #

def _emit(
    kind: PatternKind, df: pd.DataFrame, geom: dict[str, np.ndarray],
    idx: int, direction: int, note: str = "", meta: dict | None = None,
) -> PriceActionEvent:
    n = len(df)
    return PriceActionEvent(
        kind=kind,
        bar_time=df.index[idx],
        bar_index_from_end=n - 1 - idx,
        direction=direction,
        close=float(geom["c"][idx]),
        extreme=float(geom["l"][idx] if direction > 0 else geom["h"][idx]),
        body=float(geom["body"][idx]),
        note=note,
        meta=meta or {},
    )


def detect_pin(df: pd.DataFrame, geom: dict[str, np.ndarray]) -> list[PriceActionEvent]:
    """SPEC §11.2 pin bar (bull and bear) using `PIN_TAIL_RATIO` / `PIN_WICK_MAX`."""
    body = geom["body"]
    upper = geom["upper"]
    lower = geom["lower"]
    # Avoid division-by-zero: a doji (body=0) cannot satisfy "body × N" comparisons.
    safe_body = np.where(body > 0, body, np.nan)

    bull_mask = (lower >= safe_body * config.PIN_TAIL_RATIO) & \
                (upper < safe_body * config.PIN_WICK_MAX)
    bear_mask = (upper >= safe_body * config.PIN_TAIL_RATIO) & \
                (lower < safe_body * config.PIN_WICK_MAX)

    out: list[PriceActionEvent] = []
    for idx in np.flatnonzero(bull_mask):
        out.append(_emit(PatternKind.PIN_BULL, df, geom, int(idx), +1,
                         note="lower wick ≥ 2× body"))
    for idx in np.flatnonzero(bear_mask):
        out.append(_emit(PatternKind.PIN_BEAR, df, geom, int(idx), -1,
                         note="upper wick ≥ 2× body"))
    return out


def detect_engulfing(df: pd.DataFrame, geom: dict[str, np.ndarray]) -> list[PriceActionEvent]:
    """SPEC §11.2 engulfing — *body* engulfs, not just range.

    Vectorised: build boolean masks for the bull/bear engulfing conditions
    over every bar, then iterate the small set of True indices to emit
    events. Phase 5a profile showed the per-bar Python loop landed in the
    top tottime hot list; this version keeps the Python work proportional
    to *matches*, not to bar count.
    """
    o, c = geom["o"], geom["c"]
    n = c.size
    if n < 2:
        return []

    prev_o = o[:-1]; prev_c = c[:-1]
    cur_o  = o[1:];  cur_c  = c[1:]
    prev_top = np.maximum(prev_o, prev_c)
    prev_bot = np.minimum(prev_o, prev_c)
    cur_top  = np.maximum(cur_o, cur_c)
    cur_bot  = np.minimum(cur_o, cur_c)
    prev_bear = prev_c < prev_o
    prev_bull = prev_c > prev_o
    cur_bull  = cur_c  > cur_o
    cur_bear  = cur_c  < cur_o
    engulfed_body = (cur_bot < prev_bot) & (cur_top > prev_top)

    bull_mask = cur_bull & prev_bear & engulfed_body
    bear_mask = cur_bear & prev_bull & engulfed_body

    out: list[PriceActionEvent] = []
    for idx in np.flatnonzero(bull_mask):
        i = int(idx) + 1            # shift back into the original bar axis
        out.append(_emit(PatternKind.ENGULF_BULL, df, geom, i, +1,
                         note="bull body engulfs prior bear body"))
    for idx in np.flatnonzero(bear_mask):
        i = int(idx) + 1
        out.append(_emit(PatternKind.ENGULF_BEAR, df, geom, i, -1,
                         note="bear body engulfs prior bull body"))
    return out


def detect_inside(df: pd.DataFrame, geom: dict[str, np.ndarray]) -> list[PriceActionEvent]:
    """SPEC §11.2 inside bar: today's high/low strictly within prior bar."""
    h, l = geom["h"], geom["l"]
    n = h.size
    out: list[PriceActionEvent] = []
    for i in range(1, n):
        if h[i] < h[i - 1] and l[i] > l[i - 1]:
            out.append(_emit(PatternKind.INSIDE, df, geom, i, 0,
                             note="inside bar"))
    return out


def detect_inside_break(
    df: pd.DataFrame, geom: dict[str, np.ndarray], lookback: int
) -> list[PriceActionEvent]:
    """SPEC §11.2 inside-bar break: within `lookback` bars after an inside
    bar, close breaks the parent bar's high (up) or low (down)."""
    h, l, c = geom["h"], geom["l"], geom["c"]
    n = h.size
    out: list[PriceActionEvent] = []
    # First scan inside bars (excluding the latest `lookback` so a break
    # candidate can exist).
    for i in range(1, n):
        if not (h[i] < h[i - 1] and l[i] > l[i - 1]):
            continue
        parent_high = h[i - 1]
        parent_low = l[i - 1]
        # Inspect the next up-to-`lookback` bars for a directional close.
        for j in range(i + 1, min(i + 1 + lookback, n)):
            if c[j] > parent_high:
                out.append(_emit(
                    PatternKind.INSIDE_BREAK_UP, df, geom, j, +1,
                    note=f"inside-bar break up, parent index {i}",
                    meta={"parent_index": int(i)},
                ))
                break
            if c[j] < parent_low:
                out.append(_emit(
                    PatternKind.INSIDE_BREAK_DOWN, df, geom, j, -1,
                    note=f"inside-bar break down, parent index {i}",
                    meta={"parent_index": int(i)},
                ))
                break
    return out


def detect_three_bar_reversal(
    df: pd.DataFrame, geom: dict[str, np.ndarray]
) -> list[PriceActionEvent]:
    """SPEC §11.2: three bars forming a clean reversal.

    Bullish: bar[i-2] bear, bar[i-1] bear, bar[i] bull AND close[i] > high[i-1].
    Bearish: bar[i-2] bull, bar[i-1] bull, bar[i] bear AND close[i] < low[i-1].
    """
    o, c, h, l = geom["o"], geom["c"], geom["h"], geom["l"]
    n = c.size
    out: list[PriceActionEvent] = []
    for i in range(2, n):
        if (c[i - 2] < o[i - 2] and c[i - 1] < o[i - 1]
                and c[i] > o[i] and c[i] > h[i - 1]):
            out.append(_emit(PatternKind.THREE_BAR_REVERSAL_UP, df, geom, i, +1,
                             note="3-bar bullish reversal"))
        elif (c[i - 2] > o[i - 2] and c[i - 1] > o[i - 1]
              and c[i] < o[i] and c[i] < l[i - 1]):
            out.append(_emit(PatternKind.THREE_BAR_REVERSAL_DOWN, df, geom, i, -1,
                             note="3-bar bearish reversal"))
    return out


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def detect_all(
    m15: pd.DataFrame,
    keep_recent: int = config.PA_KEEP_RECENT,
    inside_break_lookback: int = config.INSIDE_BREAK_LOOKBACK,
) -> list[PriceActionEvent]:
    """Run every detector and return the most-recent *keep_recent* events.

    Events are ordered by their bar index (oldest first).
    """
    if m15.empty:
        return []
    geom = _geometry(m15)
    events: list[PriceActionEvent] = []
    events.extend(detect_pin(m15, geom))
    events.extend(detect_engulfing(m15, geom))
    events.extend(detect_inside(m15, geom))
    events.extend(detect_inside_break(m15, geom, inside_break_lookback))
    events.extend(detect_three_bar_reversal(m15, geom))

    # Order by bar age ascending (oldest first), then keep only the latest N.
    events.sort(key=lambda e: -e.bar_index_from_end)
    return events[-keep_recent:]
