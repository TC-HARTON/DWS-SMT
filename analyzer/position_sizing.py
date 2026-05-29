"""Position sizing — recommended lot from account equity.

Fixed-fractional "lot ladder": add ``config.LOT_BASE`` lots for every
``config.LOT_EQUITY_STEP`` of account equity, floored to the 0.01 lot grid and
clamped to ``[LOT_MIN, LOT_MAX]``. Validated on the 16-year XAUUSD M15 backtest
(start 0.01 lot @ 100k JPY): compounds size with the account while keeping peak
drawdown small (~6%). This is the single source of truth for the dashboard's
推奨ロット readout and for a future auto-trade EA, so the rule lives in exactly
one place.
"""

from __future__ import annotations

import math

import config


def recommended_lot(equity: float | None) -> float:
    """Recommended trade size in lots for the current account *equity*.

    ``lot = clamp(LOT_BASE * floor(equity / LOT_EQUITY_STEP), LOT_MIN, LOT_MAX)``,
    rounded to the 0.01 lot grid.

    Args:
        equity: Current account equity in the account currency (JPY here).
            ``None``, non-finite, zero or negative equity returns ``LOT_MIN``.

    Returns:
        The recommended lot size (e.g. ``0.01`` at 100k, ``0.10`` at 1M,
        capped at ``LOT_MAX``).
    """
    if equity is None or not math.isfinite(equity) or equity <= 0.0:
        return config.LOT_MIN
    steps = math.floor(equity / config.LOT_EQUITY_STEP)
    raw = config.LOT_BASE * steps
    lot = max(config.LOT_MIN, min(config.LOT_MAX, raw))
    return round(lot, 2)
