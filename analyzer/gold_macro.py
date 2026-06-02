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


def _zscore_last(history: list[float], window: int) -> tuple[float, float] | None:
    """Population z-score of the LAST level over the trailing *window*.

    Returns ``(latest_value, z)`` or ``None`` when the trailing window has
    fewer than 2 points or zero variance (a flat series → z 0 is returned, not
    None, because a flat driver is informative: it is simply neutral)."""
    if not history:
        return None
    window_vals = history[-window:] if window > 0 else list(history)
    if len(window_vals) < 2:
        return None
    n = len(window_vals)
    mean = sum(window_vals) / n
    var = sum((v - mean) ** 2 for v in window_vals) / n      # population variance
    latest = window_vals[-1]
    if var <= 0.0:
        return latest, 0.0
    return latest, (latest - mean) / math.sqrt(var)


def _band_for(score: float | None) -> str:
    """Map a composite score to its interpretation band."""
    if score is None:
        return "データ待ち"
    thr = config.GOLD_MACRO_BAND_THRESHOLD
    if score >= thr:
        return "構造的追風"
    if score <= -thr:
        return "構造的逆風"
    return "中立"


def compute_gold_macro_score(
    histories: dict[str, list[float]],
    *,
    window: int = config.GOLD_MACRO_WINDOW,
    as_of: str,
    stale: bool,
) -> GoldMacroSnapshot:
    """Fuse the driver level histories into a GoldMacroSnapshot.

    Each driver is z-scored over the trailing *window* of its LEVEL, clamped to
    ``±GOLD_MACRO_Z_CLAMP``, sign-adjusted to gold's direction, then the present
    drivers are equal-weighted and the mean rescaled from the clamp range onto
    ``-10..+10``. Drivers with no usable history are dropped from the mean. With
    zero usable drivers the score is ``None``.

    Args:
        histories: ``{driver_key: [level, ...]}`` newest-LAST per driver.
        window: trailing observation count for the z-score.
        as_of: ISO date of the newest observation (for display).
        stale: True when served from cache after a fetch failure.
    """
    clamp = config.GOLD_MACRO_Z_CLAMP
    contribs: list[GoldDriverContribution] = []
    for d in GOLD_DRIVERS:
        res = _zscore_last(histories.get(d.key) or [], window)
        if res is None:
            continue
        value, z = res
        signed = d.sign_gold * max(-clamp, min(clamp, z))
        contribs.append(GoldDriverContribution(
            key=d.key, label_ja=d.label_ja, value=value, z=z,
            signed_z=signed, sign_gold=d.sign_gold,
        ))

    if not contribs:
        return GoldMacroSnapshot(
            score=None, band=_band_for(None), contributions=(),
            n_drivers=0, window=window, as_of=as_of, stale=stale,
        )

    raw = sum(c.signed_z for c in contribs) / len(contribs)
    score = max(-10.0, min(10.0, raw / clamp * 10.0))
    return GoldMacroSnapshot(
        score=score, band=_band_for(score), contributions=tuple(contribs),
        n_drivers=len(contribs), window=window, as_of=as_of, stale=stale,
    )
