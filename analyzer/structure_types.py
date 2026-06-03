"""Shared dataclasses for structure levels (SPEC §8).

These types are produced by ``analyzer.line_reader``, which parses MT5 EA
JSON output (user-drawn TL/SR, rectangles, channels, fibonacci, text
annotations). SPEC §8.1 calls these *primary* signals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class LevelSource(str, Enum):
    """SPEC §8.1 trust hierarchy."""

    EA_USER = "ea_user"           # 一次: user-drawn line from MT5 EA
    AUTO_DETECT = "auto_detect"   # 二次: Python-derived
    AUTO_VWAP = "auto_vwap"       # special-case secondary: rolling, not a fixed level


class LevelKind(str, Enum):
    # EA-sourced
    TREND_LINE = "trend_line"
    HORIZONTAL = "horizontal"
    RECTANGLE = "rectangle"
    CHANNEL_MAIN = "channel_main"
    CHANNEL_PARALLEL = "channel_parallel"
    FIBONACCI = "fibonacci"
    TEXT_NOTE = "text_note"
    # Auto-detected
    PREV_DAY_HIGH = "pdh"
    PREV_DAY_LOW = "pdl"
    PREV_WEEK_HIGH = "pwh"
    PREV_WEEK_LOW = "pwl"
    PREV_MONTH_HIGH = "pmh"
    PREV_MONTH_LOW = "pml"
    ROUND_NUMBER = "round"
    SWING_HIGH = "swing_high"
    SWING_LOW = "swing_low"
    SESSION_HIGH = "session_high"
    SESSION_LOW = "session_low"
    VWAP = "vwap"


# Trader-named buckets used by the UI badges. Derived by line_reader from
# the EA name prefix per SPEC §9.3 ("R_", "S_", "TL_up_", "TL_dn_", ...).
LevelCategory = Literal[
    "resistance",   # R_, R1_, R2_
    "support",      # S_, S1_, S2_
    "trend_up",     # TL_up_
    "trend_down",   # TL_dn_
    "supply_zone",  # zone_supply
    "demand_zone",  # zone_demand
    "channel",
    "fibonacci",
    "note",
    "previous",     # PDH/PDL/PWH/PWL/PMH/PML
    "round",
    "swing",
    "session",
    "vwap",
    "other",
]


@dataclass(frozen=True)
class StructureLevel:
    """A single price-line structure level shown on a symbol panel.

    For non-horizontal entities (trendlines, channels) the ``price`` field
    holds the value extrapolated to the most recent tick time, supplied by
    the EA. Sloped lines additionally include their original endpoints in
    :attr:`meta` so the UI can show "TL up D1: $3,242 (+7.96/d)".
    """

    symbol: str                              # SPEC base symbol name
    name: str                                # original object/level name
    kind: LevelKind
    category: LevelCategory
    source: LevelSource
    price: float                             # current value at this moment
    importance: int = 1                      # 1=weak/default, 2=major, 3=strong
    color: str | None = None                 # hex like "#FF0000" if EA provided
    tf: str | None = None                    # describing TF if encoded in name
    meta: dict = field(default_factory=dict) # original geometry / per-kind extras

    def __post_init__(self) -> None:
        if not isinstance(self.price, (int, float)):
            raise TypeError(f"price must be numeric, got {type(self.price).__name__}")
        if self.importance not in (1, 2, 3):
            raise ValueError(f"importance must be 1|2|3, got {self.importance}")
