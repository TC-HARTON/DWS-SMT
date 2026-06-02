"""GoldMacroScore — an XAUUSD-specific macro composite (pure, testable).

Fuses four daily FRED drivers into one ``-10..+10`` score on the same scale as
the dashboard BIAS, the gold analogue of the Buffett Indicator. Each driver is
z-scored over a trailing window of its LEVEL, sign-adjusted to gold's expected
direction, clamped, then equal-weighted and rescaled. Equal weighting (no
fitting) is deliberate: weight-fitting on history is the primary overfitting
risk, so the prototype stays untuned and its edge is decided by the offline
IC + OOS validation, not by in-sample tuning.

This module is pure — no network, no MT5, no I/O — so it is fully unit-testable.
``MacroEngine.fetch_gold_drivers`` supplies the level histories; the analysis
loop calls :func:`compute_gold_macro_score` and stores the snapshot.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import config


@dataclass(frozen=True)
class GoldDriver:
    """One macro driver feeding the composite."""

    key: str
    series_id: str
    sign_gold: int          # +1 = rising value is bullish gold, -1 = bearish
    label_ja: str


@dataclass(frozen=True)
class GoldDriverContribution:
    """A driver's resolved contribution for one snapshot."""

    key: str
    label_ja: str
    value: float            # latest level
    z: float                # raw z-score over the window
    signed_z: float         # sign_gold * clamp(z)
    sign_gold: int


@dataclass(frozen=True)
class GoldMacroSnapshot:
    """The fused gold-macro composite at one point in time."""

    score: float | None     # -10..+10, or None when no driver is usable
    band: str               # 構造的追風 / 中立 / 構造的逆風 / データ待ち
    contributions: tuple[GoldDriverContribution, ...]
    n_drivers: int
    window: int
    as_of: str
    stale: bool
    generated_at: float = field(default_factory=time.time)


# The four-driver registry. Series ids live in config so they are tunable in
# one place; the signs encode the economic direction and never change.
GOLD_DRIVERS: tuple[GoldDriver, ...] = (
    GoldDriver("real_yield", config.MACRO_FRED_REALYIELD_SERIES, -1, "米10年実質金利"),
    GoldDriver("breakeven", config.MACRO_FRED_BREAKEVEN_SERIES, +1, "期待インフレ(10Y)"),
    GoldDriver("vix", config.MACRO_FRED_VIX_SERIES, +1, "リスク(VIX)"),
    GoldDriver("dxy", config.MACRO_FRED_DXY_SERIES, -1, "米ドル(広義)"),
)
