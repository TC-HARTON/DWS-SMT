"""Signal validation layer (precision-optimization spec, Section A).

Re-runs the parameter-free DWS-SMT signal over a deep history and reduces the
resulting trade list to robust out-of-sample statistics: win rate with a Wilson
confidence interval, profit factor, expectancy, max drawdown, average MAE,
3-period stability, and a trend/range regime split. Each (symbol, base TF) gets
a confidence tier so the dashboard can show whether a signal has a real,
stable edge or whether the short on-screen back-test is just noise.

Because DWS-SMT and BIAS are rule-based and parameter-free there is nothing to
"train" — validation here means evaluating the fixed rule on a far larger
sample than the 96-bar on-screen window and checking the edge holds across
sub-periods.

Everything in this module is pure / side-effect free except
:class:`SignalValidator.compute`, which fetches rates through the connector.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd

import config
from analyzer import dws_smt, indicators
from analyzer.dws_smt import DwsSmtTrade

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SubPeriodStats:
    """Win rate + expectancy for one third of the trade sequence."""

    win_rate: float       # 0..1
    expectancy: float     # net points per trade
    n_trades: int


@dataclass(frozen=True)
class RegimeStats:
    """Win rate + expectancy for trades entered in one ADX regime."""

    win_rate: float       # 0..1
    expectancy: float     # net points per trade
    n_trades: int


@dataclass(frozen=True)
class ValidationCore:
    """The full statistic bundle for one signal evaluation."""

    n_trades: int
    win_rate: float
    ci_low: float
    ci_high: float
    profit_factor: float
    expectancy: float
    max_drawdown: float
    avg_mae: float
    thirds: tuple[SubPeriodStats, SubPeriodStats, SubPeriodStats]
    regime_trend: RegimeStats
    regime_range: RegimeStats
    tier: str             # "信頼" | "要注意" | "データ不足"


@dataclass(frozen=True)
class ValidationStats:
    """Validation result for one (symbol, base timeframe).

    ``macro_filtered`` is the same object as ``raw`` in this layer — the macro
    filter does not exist yet (precision-optimization spec, Section A.4). The
    macro-layer plan replaces how ``macro_filtered`` triggers are selected.
    """

    symbol: str
    base_tf: str
    raw: ValidationCore
    macro_filtered: ValidationCore


@dataclass(frozen=True)
class ValidationSnapshot:
    """One full validation pass across every symbol and DWS base timeframe."""

    generated_at: float
    compute_ms: float
    by_symbol: dict[str, dict[str, ValidationStats]]


# --------------------------------------------------------------------------- #
# Statistics — pure helpers
# --------------------------------------------------------------------------- #

def wilson_interval(wins: int, n: int, z: float = config.VALIDATION_CI_Z
                    ) -> tuple[float, float]:
    """Wilson score confidence interval for a binomial win rate.

    The Wilson interval is well-behaved for small ``n`` and never escapes
    ``[0, 1]`` — unlike the normal approximation, which is why it is used here
    instead of ``p ± z·sqrt(p(1-p)/n)``.

    Args:
        wins: number of winning trades.
        n: total number of trades.
        z: standard-normal quantile (1.96 → 95 % two-sided).

    Returns:
        ``(low, high)``. ``n == 0`` returns ``(0.0, 1.0)``.
    """
    if n <= 0:
        return 0.0, 1.0
    p = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    return max(0.0, center - margin), min(1.0, center + margin)
