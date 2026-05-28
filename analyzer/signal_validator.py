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
from dataclasses import dataclass, replace

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
class RecentTrigger:
    """One DWS-SMT trigger from the LIVE deep-history evaluation.

    Sourced straight from the connected MT5 broker (whatever broker the user
    is on) via the validator's deep fetch — so the dashboard's trigger history
    is always current and broker-agnostic. ``entry_ms`` is the entry bar's
    epoch-ms (UTC); ``direction`` is +1 long / -1 short.

    The panel is named トリガー履歴 ("trigger history"), so it logs a trigger
    the moment it fires — open positions included. ``is_open`` flags the
    still-running trigger (at most one per symbol/TF — the current position).
    For a closed trigger ``net_pts`` is realised P/L in points after the bar
    spread; for an open one it is the floating P/L marked to the most recent
    close (the dashboard shows it greyed as 保有中 and excludes it from the
    realised win-rate / PF / cumulative stats).
    """

    entry_ms: int
    direction: int
    net_pts: float
    is_open: bool = False


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
    # Last N closed triggers (newest first) for the dashboard history table.
    # Empty by default so call sites that don't supply timestamps still work.
    recent_triggers: tuple[RecentTrigger, ...] = ()


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


def max_drawdown(pnls: list[float]) -> float:
    """Largest peak-to-trough drop of the cumulative equity curve.

    Args:
        pnls: per-trade net P/L in chronological order.

    Returns:
        Drawdown magnitude (``>= 0``). Empty input or a monotonically rising
        curve returns ``0.0``.
    """
    peak = 0.0
    equity = 0.0
    worst = 0.0
    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
        drop = peak - equity
        if drop > worst:
            worst = drop
    return worst


def summarize_pnls(pnls: list[float]) -> dict[str, float]:
    """Reduce a chronological net-P/L list to headline statistics.

    Returns a dict with ``n``, ``win_rate`` (0..1), ``profit_factor``
    (``inf`` when there are no losing trades, ``0.0`` when there are no
    trades at all), ``expectancy`` (mean net P/L) and ``max_drawdown``.
    """
    n = len(pnls)
    if n == 0:
        return {"n": 0, "win_rate": 0.0, "profit_factor": 0.0,
                "expectancy": 0.0, "max_drawdown": 0.0}
    wins = [p for p in pnls if p > 0.0]
    losses = [p for p in pnls if p < 0.0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    if gross_loss > 0.0:
        profit_factor = gross_win / gross_loss
    elif gross_win > 0.0:
        profit_factor = math.inf
    else:
        profit_factor = 0.0
    return {
        "n": n,
        "win_rate": len(wins) / n,
        "profit_factor": profit_factor,
        "expectancy": sum(pnls) / n,
        "max_drawdown": max_drawdown(pnls),
    }


TIER_TRUSTED = "信頼"
TIER_CAUTION = "要注意"
TIER_INSUFFICIENT = "データ不足"


def breakeven_win_rate(pnls: list[float]) -> float:
    """Win rate at which the realised avg win / avg loss nets to zero.

    ``breakeven = avg_loss / (avg_win + avg_loss)`` using magnitudes. With no
    losing trades the breakeven is ``0.0`` (any win rate profits); with no
    winning trades it is ``1.0``.
    """
    wins = [p for p in pnls if p > 0.0]
    losses = [abs(p) for p in pnls if p < 0.0]
    if not losses:
        return 0.0
    if not wins:
        return 1.0
    avg_win = sum(wins) / len(wins)
    avg_loss = sum(losses) / len(losses)
    return avg_loss / (avg_win + avg_loss)


def classify_tier(
    *,
    n_trades: int,
    ci_low: float,
    breakeven: float,
    thirds_expectancy: list[float],
) -> str:
    """Map a validation result onto one of the three confidence tiers.

    * ``データ不足`` — fewer than :data:`config.VALIDATION_MIN_TRADES` trades.
    * ``信頼`` — the win-rate CI lower bound clears the breakeven win rate AND
      every sub-period (third) has positive expectancy.
    * ``要注意`` — everything else: an edge that is unstable, marginal, or
      absent. The numeric metrics shown alongside disambiguate.
    """
    if n_trades < config.VALIDATION_MIN_TRADES:
        return TIER_INSUFFICIENT
    if ci_low > breakeven and all(e > 0.0 for e in thirds_expectancy):
        return TIER_TRUSTED
    return TIER_CAUTION


def _sub_period(pnls: list[float]) -> SubPeriodStats:
    """Build a :class:`SubPeriodStats` from one slice of the net-P/L list."""
    s = summarize_pnls(pnls)
    return SubPeriodStats(win_rate=s["win_rate"], expectancy=s["expectancy"],
                          n_trades=int(s["n"]))


def _regime(pnls: list[float]) -> RegimeStats:
    """Build a :class:`RegimeStats` from the net-P/L list of one ADX regime."""
    s = summarize_pnls(pnls)
    return RegimeStats(win_rate=s["win_rate"], expectancy=s["expectancy"],
                       n_trades=int(s["n"]))


def evaluate_trades(
    trades: tuple[DwsSmtTrade, ...],
    *,
    spread_pts: np.ndarray,
    adx: np.ndarray,
    point: float,
) -> ValidationCore:
    """Reduce one DWS window's trades to a :class:`ValidationCore`.

    Args:
        trades: every trade of the window (open trades are ignored — only
            realised P/L is validated).
        spread_pts: per-bar broker spread in points, aligned to the window
            (index == trade ``entry_idx``).
        adx: per-bar ADX of the base timeframe, aligned to the window.
        point: broker ``point`` size, to convert price moves to points.

    Returns:
        A :class:`ValidationCore`. An empty trade list yields a zeroed core
        with tier ``データ不足``.
    """
    point = point if point > 0.0 else 1.0
    closed = [t for t in trades if not t.is_open]

    nets: list[float] = []
    maes: list[float] = []
    trend_nets: list[float] = []
    range_nets: list[float] = []
    for t in closed:
        cost = float(spread_pts[t.entry_idx]) if t.entry_idx < spread_pts.size else 0.0
        net = t.points / point - cost
        nets.append(net)
        maes.append(t.mae / point)
        bar_adx = float(adx[t.entry_idx]) if t.entry_idx < adx.size else 0.0
        if bar_adx >= config.BIAS_REGIME_ADX_HIGH:
            trend_nets.append(net)
        else:
            range_nets.append(net)

    summary = summarize_pnls(nets)
    n = int(summary["n"])
    wins = sum(1 for p in nets if p > 0.0)
    ci_low, ci_high = wilson_interval(wins, n)

    # Chronological 3-way split (closed trades are already in entry order).
    thirds_lists = _split_three(nets)
    thirds = (_sub_period(thirds_lists[0]),
              _sub_period(thirds_lists[1]),
              _sub_period(thirds_lists[2]))

    tier = classify_tier(
        n_trades=n,
        ci_low=ci_low,
        breakeven=breakeven_win_rate(nets),
        thirds_expectancy=[t.expectancy for t in thirds],
    )
    return ValidationCore(
        n_trades=n,
        win_rate=summary["win_rate"],
        ci_low=ci_low,
        ci_high=ci_high,
        profit_factor=summary["profit_factor"],
        expectancy=summary["expectancy"],
        max_drawdown=summary["max_drawdown"],
        avg_mae=(sum(maes) / len(maes)) if maes else 0.0,
        thirds=thirds,
        regime_trend=_regime(trend_nets),
        regime_range=_regime(range_nets),
        tier=tier,
    )


def _split_three(items: list[float]) -> tuple[list[float], list[float], list[float]]:
    """Split a list into three contiguous, near-equal slices.

    A remainder is pushed onto the later slices so the first slice is never
    larger than the last (keeps the "front-loaded edge" check honest).
    """
    n = len(items)
    base = n // 3
    extra = n - base * 3            # 0, 1 or 2 — added to the last slices
    cut1 = base
    cut2 = base + base + (1 if extra >= 1 else 0)
    # extra == 2 also lengthens the final slice implicitly (slice to end).
    return items[:cut1], items[cut1:cut2], items[cut2:]


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

# Every timeframe any DWS-SMT stack references, with its MT5 constant.
_TF_CONST: dict[str, int] = {
    tf.label: tf.mt5_const
    for tf in (*config.TIMEFRAMES, *config.STRUCTURE_TFS)
}
_NEEDED_TFS: tuple[str, ...] = tuple(
    sorted({tf for stack in config.DWS_SMT_STACKS.values() for tf in stack})
)


class SignalValidator:
    """Evaluate the DWS-SMT signal over deep history for every symbol.

    The connector only needs a ``fetch_rates_parallel(bases, timeframes)``
    method, so a lightweight fake can drive it in tests.
    """

    def __init__(
        self,
        connector,
        *,
        history_bars: int = config.VALIDATION_HISTORY_BARS,
        fetch_gap_sec: float = config.VALIDATION_FETCH_GAP_SEC,
    ) -> None:
        self._connector = connector
        self._history_bars = history_bars
        self._fetch_gap_sec = fetch_gap_sec
        # Deep-history fetch specs. Bar count is per-timeframe
        # (config.VALIDATION_TF_BARS): the base TFs get the full window, but
        # the higher row TFs (D1/W1) are capped to what the broker holds —
        # requesting years of D1/W1 history triggers a slow empty broker sync
        # that holds the connector lock and freezes the dashboard.
        self._deep_specs = tuple(
            config.TimeframeSpec(
                label, _TF_CONST[label], 0,
                config.VALIDATION_TF_BARS.get(label, history_bars),
            )
            for label in _NEEDED_TFS
            if label in _TF_CONST
        )

    def compute(
        self,
        bases: list[str],
        broker_meta: dict[str, dict[str, float]],
    ) -> ValidationSnapshot:
        """Run one validation pass over *bases*.

        Deep history is fetched ONE SYMBOL AT A TIME with a
        :data:`config.VALIDATION_FETCH_GAP_SEC` pause between symbols. The
        connector serialises every MT5 call through a single lock, and a cold
        deep-history fetch can hold that lock (and the GIL) for seconds;
        fetching per symbol with a gap lets the live 0.5 s price tick and 5 s
        analysis pass interleave instead of being starved for the whole pass.

        Args:
            bases: symbol base names to validate.
            broker_meta: ``{base: {"point": float, ...}}`` — used to convert
                price moves to points. A missing entry falls back to a point
                size of 1.0 (the stats stay internally consistent).

        Returns:
            A :class:`ValidationSnapshot`. Symbols/timeframes with no usable
            history are simply absent from ``by_symbol`` — never raised.
        """
        t0 = time.perf_counter()
        by_symbol: dict[str, dict[str, ValidationStats]] = {}

        for i, base in enumerate(bases):
            if i > 0 and self._fetch_gap_sec > 0.0:
                # Yield the connector lock / GIL to the live dashboard loop.
                time.sleep(self._fetch_gap_sec)
            rates = self._connector.fetch_rates_parallel([base], self._deep_specs)
            frames = {
                tf: rates[(base, tf)]
                for tf in _NEEDED_TFS
                if (base, tf) in rates and not rates[(base, tf)].empty
            }
            if not frames:
                continue
            point = float(broker_meta.get(base, {}).get("point", 1.0) or 1.0)
            try:
                per_tf = self._validate_symbol(base, frames, point)
            except (ValueError, KeyError, IndexError):
                log.exception("signal validation failed for %s", base)
                continue
            if per_tf:
                by_symbol[base] = per_tf

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return ValidationSnapshot(
            generated_at=time.time(),
            compute_ms=elapsed_ms,
            by_symbol=by_symbol,
        )

    def _validate_symbol(
        self,
        base: str,
        frames: dict[str, pd.DataFrame],
        point: float,
    ) -> dict[str, ValidationStats]:
        """Validate every DWS base timeframe for one symbol."""
        result = dws_smt.compute_symbol(frames, out_bars=self._history_bars)
        if result is None:
            return {}
        out: dict[str, ValidationStats] = {}
        for base_tf, window in result.by_base.items():
            base_df = frames.get(base_tf)
            if base_df is None or base_df.empty:
                continue
            core = self._evaluate_window(window, base_df, point)
            out[base_tf] = ValidationStats(
                symbol=base, base_tf=base_tf, raw=core, macro_filtered=core,
            )
        return out

    @staticmethod
    def _evaluate_window(window, base_df: pd.DataFrame, point: float
                         ) -> ValidationCore:
        """Build a :class:`ValidationCore` from one DWS window.

        Slices the base frame's spread column and a freshly computed ADX
        series with the same ``start`` offset ``_build_window`` used, so the
        trade ``entry_idx`` lines up with both arrays.
        """
        n_bars = len(base_df)
        emitted = window.times_ms.size
        start = max(0, n_bars - emitted)

        if "spread" in base_df.columns:
            spread_pts = base_df["spread"].to_numpy(dtype=np.float64)[start:]
        else:
            spread_pts = np.zeros(emitted, dtype=np.float64)

        high = base_df["high"].to_numpy(dtype=np.float64)[None, :]
        low = base_df["low"].to_numpy(dtype=np.float64)[None, :]
        close = base_df["close"].to_numpy(dtype=np.float64)[None, :]
        adx_2d, _, _ = indicators.adx(high, low, close, config.ADX_PERIOD)
        adx = np.nan_to_num(adx_2d[0][start:], nan=0.0)

        core = evaluate_trades(window.trades, spread_pts=spread_pts,
                               adx=adx, point=point)
        # Attach the most-recent closed triggers (newest first) so the
        # dashboard history table is fed by the LIVE broker deep fetch. The
        # entry timestamp comes from the window's per-bar epoch-ms array.
        recent = _recent_triggers_from_window(
            window, spread_pts=spread_pts, point=point,
            limit=config.VALIDATION_RECENT_TRIGGERS,
        )
        return replace(core, recent_triggers=recent)


def _recent_triggers_from_window(
    window, *, spread_pts: np.ndarray, point: float, limit: int,
) -> tuple[RecentTrigger, ...]:
    """Extract the last *limit* triggers (newest first) from a window.

    Net P/L mirrors ``evaluate_trades``: ``points/point − bar_spread``. Entry
    time is the window's epoch-ms at the trade's entry bar. The panel is a
    トリガー履歴 ("trigger history"), so every trigger is logged the moment it
    fires — the still-running position is included with ``is_open=True`` (its
    ``net_pts`` is the floating P/L marked to the latest close). The dashboard
    keeps it out of the realised win-rate / PF / cumulative tallies."""
    p = point if point > 0.0 else 1.0
    times_ms = window.times_ms
    out: list[RecentTrigger] = []
    for t in window.trades:
        ei = t.entry_idx
        if ei < 0 or ei >= times_ms.size:
            continue
        cost = float(spread_pts[ei]) if ei < spread_pts.size else 0.0
        net = t.points / p - cost
        out.append(RecentTrigger(
            entry_ms=int(times_ms[ei]),
            direction=int(t.direction),
            net_pts=float(net),
            is_open=bool(t.is_open),
        ))
    out.sort(key=lambda r: r.entry_ms, reverse=True)   # newest first
    return tuple(out[:limit])
