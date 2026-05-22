"""Unit tests for analyzer.price_action (SPEC §11)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from analyzer.price_action import PatternKind, detect_all


def _df(bars: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    """Build an M15 DataFrame from (open, high, low, close) tuples."""
    n = len(bars)
    idx = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    o = [b[0] for b in bars]
    h = [b[1] for b in bars]
    l = [b[2] for b in bars]
    c = [b[3] for b in bars]
    return pd.DataFrame({
        "open": o, "high": h, "low": l, "close": c,
        "tick_volume": np.ones(n, dtype=np.int64),
        "spread": np.zeros(n, dtype=np.int32),
        "real_volume": np.zeros(n, dtype=np.int64),
    }, index=idx)


# --------------------------------------------------------------------------- #
# Pin bar
# --------------------------------------------------------------------------- #

def test_pin_bull_detected_when_lower_wick_dominates():
    # Body open=100 close=101 (body=1). Lower wick = 100-97 = 3 (>= 2 * body).
    # Upper wick = 101.1 - 101 = 0.1 (< 0.3 * body).
    bars = [(100, 101.1, 97, 101)]
    events = detect_all(_df(bars))
    kinds = {e.kind for e in events}
    assert PatternKind.PIN_BULL in kinds


def test_pin_bear_detected_when_upper_wick_dominates():
    bars = [(101, 104, 100.9, 100)]   # body=1, upper=3, lower=0.1
    events = detect_all(_df(bars))
    kinds = {e.kind for e in events}
    assert PatternKind.PIN_BEAR in kinds


def test_doji_is_not_pin():
    # body == 0 (open == close); the safe_body guard should suppress division.
    bars = [(100, 102, 98, 100)]
    events = detect_all(_df(bars))
    assert PatternKind.PIN_BULL not in {e.kind for e in events}
    assert PatternKind.PIN_BEAR not in {e.kind for e in events}


# --------------------------------------------------------------------------- #
# Engulfing
# --------------------------------------------------------------------------- #

def test_bullish_engulfing_must_engulf_body_not_just_range():
    # Prior bar bear: open 102, close 101 (body 101..102).
    # Current bar bull: open 100, close 103 (body 100..103) — engulfs 101..102.
    bars = [
        (102, 102.5, 100.8, 101),    # bear
        (100, 103.2, 99.8, 103),     # bull engulfing
    ]
    events = detect_all(_df(bars))
    bull_engulf = [e for e in events if e.kind == PatternKind.ENGULF_BULL]
    assert len(bull_engulf) == 1


def test_no_engulfing_when_bodies_not_engulfed():
    # The current bull body (100→100.5) is inside the prior bear body.
    bars = [
        (102, 102.5, 99, 100),
        (100, 101, 99.5, 100.5),
    ]
    events = detect_all(_df(bars))
    assert PatternKind.ENGULF_BULL not in {e.kind for e in events}


# --------------------------------------------------------------------------- #
# Inside bar and break
# --------------------------------------------------------------------------- #

def test_inside_bar_detected():
    bars = [
        (100, 105, 95, 102),    # parent
        (101, 104, 96, 103),    # inside
    ]
    events = detect_all(_df(bars))
    assert PatternKind.INSIDE in {e.kind for e in events}


def test_inside_break_up_detected_within_lookback():
    bars = [
        (100, 105, 95, 102),    # parent
        (101, 104, 96, 103),    # inside
        (103, 104, 102, 103.5), # noise (still inside parent range)
        (103, 110, 102, 106),   # close > parent_high (105) → break up
    ]
    events = detect_all(_df(bars))
    assert PatternKind.INSIDE_BREAK_UP in {e.kind for e in events}


def test_inside_break_down_detected():
    bars = [
        (100, 105, 95, 102),
        (101, 104, 96, 103),
        (98, 102, 90, 92),    # close < parent_low (95)
    ]
    events = detect_all(_df(bars))
    assert PatternKind.INSIDE_BREAK_DOWN in {e.kind for e in events}


# --------------------------------------------------------------------------- #
# 3-bar reversal
# --------------------------------------------------------------------------- #

def test_three_bar_bull_reversal():
    bars = [
        (100, 100, 95, 96),     # bear
        (96, 96, 92, 93),       # bear
        (93, 98, 92, 97),       # bull, close 97 > prior high 96 — reversal
    ]
    events = detect_all(_df(bars))
    assert PatternKind.THREE_BAR_REVERSAL_UP in {e.kind for e in events}


def test_three_bar_bear_reversal():
    bars = [
        (90, 95, 90, 94),
        (94, 99, 94, 98),
        (98, 99, 90, 91),
    ]
    events = detect_all(_df(bars))
    assert PatternKind.THREE_BAR_REVERSAL_DOWN in {e.kind for e in events}


# --------------------------------------------------------------------------- #
# Aggregate
# --------------------------------------------------------------------------- #

def test_detect_all_on_empty_dataframe_returns_empty_list():
    df = _df([])
    assert detect_all(df) == []


def test_detect_all_caps_at_keep_recent():
    # Generate many pin bars in a row.
    bars = []
    for i in range(20):
        bars.append((100, 101.1, 97, 101))
    events = detect_all(_df(bars), keep_recent=4)
    assert len(events) == 4
