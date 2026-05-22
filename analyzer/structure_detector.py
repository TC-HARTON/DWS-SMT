"""Auto-detected structure levels (SPEC §10).

Produces every secondary-tier level called out in SPEC §10 from rate
DataFrames returned by :class:`MT5Connector`. The output is the same
:class:`StructureLevel` shape used by the EA-sourced :mod:`line_reader`,
so the dashboard renders both side-by-side and confluence detection
operates on a single unified list.

Detectors run per symbol on each analysis-loop tick (5 s).  They are
intentionally stateless and side-effect free — every cycle they look at
the latest bars and emit a fresh list.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

import config
from analyzer.structure_types import (
    LevelKind,
    LevelSource,
    StructureLevel,
)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def detect_all(
    symbol: str,
    rates: dict[str, pd.DataFrame],
    current_price: float | None,
    *,
    round_step: float | None = None,
    round_rungs: int = config.ROUND_NUMBER_RUNGS,
) -> list[StructureLevel]:
    """Run every Phase-2 detector for *symbol*.

    Args:
        symbol: SPEC base symbol name (e.g. ``"XAUUSD"``).
        rates: ``{tf_label: DataFrame}`` covering at minimum D1, W1, MN1,
            M1, M15. Missing TFs are tolerated — their detectors simply
            return nothing.
        current_price: latest bid (used to centre the round-number ladder
            and to seed VWAP if M1 is absent). ``None`` → defaults to the
            last D1 close when available.
        round_step: SPEC §10.2 explicit step; ``None`` falls back to
            :data:`config.ROUND_NUMBER_STEPS` then to a price-magnitude
            heuristic.
        round_rungs: number of rungs above and below to publish.
    """
    out: list[StructureLevel] = []

    # ---- previous-period highs/lows (SPEC §10.1) ----
    out.extend(_previous_period_levels(symbol, rates))

    # ---- round numbers (SPEC §10.2) ----
    price = current_price
    if price is None and "D1" in rates and not rates["D1"].empty:
        price = float(rates["D1"]["close"].iloc[-1])
    if price is not None:
        step = round_step
        if step is None:
            step = config.ROUND_NUMBER_STEPS.get(symbol)
        if step is None:
            step = _heuristic_round_step(price)
        out.extend(_round_numbers(symbol, price, step, round_rungs))

    # ---- fractal swings (SPEC §10.1) ----
    for tf in config.FRACTAL_TFS:
        df = rates.get(tf)
        if df is None or df.empty:
            continue
        out.extend(_swing_points(symbol, tf, df,
                                 lookback=config.FRACTAL_LOOKBACK,
                                 keep=config.FRACTAL_KEEP_PER_TF))

    # ---- session highs/lows (SPEC §10.1 + §10.3) ----
    m1 = rates.get("M1")
    if m1 is not None and not m1.empty:
        out.extend(_session_highs_lows(symbol, m1))

    # ---- VWAP (SPEC §10.1: 当日出来高加重平均) ----
    if m1 is not None and not m1.empty:
        vwap = _vwap_today(m1)
        if vwap is not None:
            out.append(StructureLevel(
                symbol=symbol,
                name="VWAP_today",
                kind=LevelKind.VWAP,
                category="vwap",
                source=LevelSource.AUTO_VWAP,
                price=vwap,
                importance=2,
                tf="M1",
            ))

    return out


# --------------------------------------------------------------------------- #
# PDH / PDL / PWH / PWL / PMH / PML  (SPEC §10.1)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class _PrevSpec:
    """Tells the detector which TF and which output naming to use."""

    prefix: str               # "PD" | "PW" | "PM"
    tf: str                   # config.PREV_PERIOD_TFS[prefix]
    high_kind: LevelKind
    low_kind: LevelKind
    label_high: str
    label_low: str
    tf_label: str


_PREV_SPECS: tuple[_PrevSpec, ...] = (
    _PrevSpec("PD", "D1",  LevelKind.PREV_DAY_HIGH,   LevelKind.PREV_DAY_LOW,
              "PDH", "PDL", "D1"),
    _PrevSpec("PW", "W1",  LevelKind.PREV_WEEK_HIGH,  LevelKind.PREV_WEEK_LOW,
              "PWH", "PWL", "W1"),
    _PrevSpec("PM", "MN1", LevelKind.PREV_MONTH_HIGH, LevelKind.PREV_MONTH_LOW,
              "PMH", "PML", "MN1"),
)


def _previous_period_levels(
    symbol: str, rates: dict[str, pd.DataFrame]
) -> list[StructureLevel]:
    out: list[StructureLevel] = []
    for spec in _PREV_SPECS:
        df = rates.get(spec.tf)
        if df is None or len(df) < 2:
            continue
        # The last row is the *current* bar; we want the bar before it
        # (the most-recently-closed period).
        prev = df.iloc[-2]
        out.append(StructureLevel(
            symbol=symbol, name=spec.label_high, kind=spec.high_kind,
            category="previous", source=LevelSource.AUTO_DETECT,
            price=float(prev["high"]), importance=2, tf=spec.tf_label,
        ))
        out.append(StructureLevel(
            symbol=symbol, name=spec.label_low, kind=spec.low_kind,
            category="previous", source=LevelSource.AUTO_DETECT,
            price=float(prev["low"]), importance=2, tf=spec.tf_label,
        ))
    return out


# --------------------------------------------------------------------------- #
# Round numbers  (SPEC §10.2)
# --------------------------------------------------------------------------- #

def _heuristic_round_step(price: float) -> float:
    """Best-effort step for symbols not explicitly listed in config."""
    if price >= 1000:
        return 50.0          # gold-like
    if price >= 50:
        return 0.5           # JPY pairs
    if price >= 5:
        return 0.05
    return 0.01              # majors


def _round_numbers(
    symbol: str, price: float, step: float, rungs: int
) -> list[StructureLevel]:
    if step <= 0:
        return []
    centre = round(price / step) * step
    out: list[StructureLevel] = []
    for k in range(-rungs, rungs + 1):
        rung_price = centre + k * step
        # The rung at the exact current bucket is the most relevant — mark
        # it as importance 2; further rungs are weak (importance 1).
        importance = 2 if k == 0 else 1
        out.append(StructureLevel(
            symbol=symbol,
            name=f"R{step:g}_{rung_price:g}",
            kind=LevelKind.ROUND_NUMBER,
            category="round",
            source=LevelSource.AUTO_DETECT,
            price=float(rung_price),
            importance=importance,
            meta={"step": step, "rung_offset": k},
        ))
    return out


# --------------------------------------------------------------------------- #
# Fractal swing points  (SPEC §10.1)
# --------------------------------------------------------------------------- #

def _swing_points(
    symbol: str,
    tf_label: str,
    df: pd.DataFrame,
    *,
    lookback: int,
    keep: int,
) -> list[StructureLevel]:
    """Bill Williams 5-bar (default) fractal extraction.

    Drops the last *lookback* bars because their fractal status cannot be
    decided yet without future data.
    """
    n = len(df)
    if n < 2 * lookback + 1:
        return []
    high = df["high"].to_numpy(dtype=np.float64)
    low = df["low"].to_numpy(dtype=np.float64)

    # Vectorised fractal detection: bar i is a swing-high iff high[i] is
    # strictly greater than high[i-k] and high[i+k] for every k in 1..lookback.
    # We build masks for each k and AND them together.
    is_high = np.ones(n, dtype=bool)
    is_low = np.ones(n, dtype=bool)
    for k in range(1, lookback + 1):
        is_high &= np.concatenate([np.zeros(k, dtype=bool), high[k:] > high[:-k]])
        is_high &= np.concatenate([high[:-k] > high[k:], np.zeros(k, dtype=bool)])
        is_low &= np.concatenate([np.zeros(k, dtype=bool), low[k:] < low[:-k]])
        is_low &= np.concatenate([low[:-k] < low[k:], np.zeros(k, dtype=bool)])

    # The most recent `lookback` bars cannot have completed fractals yet.
    is_high[-lookback:] = False
    is_low[-lookback:] = False

    out: list[StructureLevel] = []
    for idx in np.flatnonzero(is_high)[-keep:]:
        out.append(StructureLevel(
            symbol=symbol, name=f"SwingH_{tf_label}_{idx}",
            kind=LevelKind.SWING_HIGH, category="swing",
            source=LevelSource.AUTO_DETECT,
            price=float(high[idx]), importance=1, tf=tf_label,
            meta={"bar_index_from_end": int(n - 1 - idx)},
        ))
    for idx in np.flatnonzero(is_low)[-keep:]:
        out.append(StructureLevel(
            symbol=symbol, name=f"SwingL_{tf_label}_{idx}",
            kind=LevelKind.SWING_LOW, category="swing",
            source=LevelSource.AUTO_DETECT,
            price=float(low[idx]), importance=1, tf=tf_label,
            meta={"bar_index_from_end": int(n - 1 - idx)},
        ))
    return out


# --------------------------------------------------------------------------- #
# Session highs / lows  (SPEC §10.1 + §10.3)
# --------------------------------------------------------------------------- #

# Cached UTC offset; the canonical value lives in :data:`config.JST_OFFSET_HOURS`.
_JST_OFFSET_HOURS = config.JST_OFFSET_HOURS


def _session_mask_utc(times_utc: pd.DatetimeIndex, sess: config.SessionSpec) -> np.ndarray:
    """Boolean mask: True where the bar's JST hour falls inside *sess*.

    Defensively converts to UTC if the caller hands in a non-UTC tz-aware
    index — the +9 h JST offset is only correct relative to UTC.
    """
    if times_utc.tz is None:
        raise ValueError("times must be tz-aware")
    if str(times_utc.tz) != "UTC":
        times_utc = times_utc.tz_convert("UTC")
    jst_hour = ((times_utc.hour + _JST_OFFSET_HOURS) % 24).to_numpy()
    if sess.start_jst <= sess.end_jst:
        return (jst_hour >= sess.start_jst) & (jst_hour < sess.end_jst)
    # Wrapping session (e.g. NY: 21:00–06:00 JST).
    return (jst_hour >= sess.start_jst) | (jst_hour < sess.end_jst)


def _session_highs_lows(symbol: str, m1: pd.DataFrame) -> list[StructureLevel]:
    """Produce per-session H/L *for the latest occurrence of each session*.

    SPEC §10.1 wants "セッション高安 (Asia/Europe/NY)" updated at each
    session's end. Implementation:
      * for each session, take the M1 bars that fall in the most recent
        contiguous occurrence and emit the high/low of that slice
    """
    times = m1.index
    if not isinstance(times, pd.DatetimeIndex) or times.tz is None:
        return []
    out: list[StructureLevel] = []
    high_arr = m1["high"].to_numpy(dtype=np.float64)
    low_arr = m1["low"].to_numpy(dtype=np.float64)

    for sess in config.SESSIONS:
        mask = _session_mask_utc(times, sess)
        if not mask.any():
            continue
        # Find the latest contiguous run of in-session bars.
        last_end = mask.size - 1
        while last_end >= 0 and not mask[last_end]:
            last_end -= 1
        if last_end < 0:
            continue
        last_start = last_end
        while last_start > 0 and mask[last_start - 1]:
            last_start -= 1

        sess_high = float(high_arr[last_start: last_end + 1].max())
        sess_low = float(low_arr[last_start: last_end + 1].min())
        out.append(StructureLevel(
            symbol=symbol, name=f"{sess.name}H", kind=LevelKind.SESSION_HIGH,
            category="session", source=LevelSource.AUTO_DETECT,
            price=sess_high, importance=1, tf="M1",
            meta={"session": sess.name},
        ))
        out.append(StructureLevel(
            symbol=symbol, name=f"{sess.name}L", kind=LevelKind.SESSION_LOW,
            category="session", source=LevelSource.AUTO_DETECT,
            price=sess_low, importance=1, tf="M1",
            meta={"session": sess.name},
        ))
    return out


# --------------------------------------------------------------------------- #
# VWAP  (SPEC §10.1)
# --------------------------------------------------------------------------- #

def _vwap_today(m1: pd.DataFrame) -> float | None:
    """Volume-weighted average price for the current UTC trading day.

    Use ``tick_volume`` because most retail brokers (Exness included) do
    not publish ``real_volume`` for FX/CFD. SPEC §10.1 says "当日出来高
    加重平均"; we treat the calendar day in UTC as "今日" — switching
    points naturally with the daily roll-over MT5 already uses.
    """
    times = m1.index
    if not isinstance(times, pd.DatetimeIndex) or times.tz is None:
        return None
    today = times[-1].normalize()              # 00:00:00 UTC of the latest bar
    mask = times >= today
    if not mask.any():
        return None
    close = m1["close"].to_numpy(dtype=np.float64)[mask]
    high = m1["high"].to_numpy(dtype=np.float64)[mask]
    low = m1["low"].to_numpy(dtype=np.float64)[mask]
    vol = m1["tick_volume"].to_numpy(dtype=np.float64)[mask]
    if vol.sum() <= 0:
        return None
    typical = (high + low + close) / 3.0
    return float(np.sum(typical * vol) / vol.sum())
