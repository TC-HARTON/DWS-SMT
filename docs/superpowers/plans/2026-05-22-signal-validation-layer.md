# Signal Validation Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an always-on signal-validation layer that evaluates BIAS/DWS-SMT
signals over deep history and shows each DWS base timeframe an out-of-sample
confidence tier (信頼 / 要注意 / データ不足) plus win-rate CI, PF, expectancy,
drawdown, MAE, 3-period stability, and trend/range regime breakdown.

**Architecture:** A new off-thread `validation` schedule (5 min) in the analysis
loop fetches deep history via the existing parallel fetcher, re-runs the
existing `dws_smt.compute_symbol` over that deep window, and reduces the trade
list to robust statistics. Results land in `LatestState` as a new snapshot and
ride the existing WebSocket light/full split. The DWS panel renders a new
confidence block.

**Tech Stack:** Python 3.11, numpy, pandas, scipy (existing); vanilla JS canvas
front end (existing); pytest.

**Spec:** `docs/superpowers/specs/2026-05-22-precision-optimization-design.md`
(Section A).

---

## Pre-flight notes for the implementer

- **Python interpreter:** the bare `python` command is a broken MS Store stub.
  Always use `C:\Users\ohuch\AppData\Local\Python\bin\python.exe`.
  Test command form: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest tests/... -v`
- **Git:** this project is NOT under git version control. Each "Commit" step
  below is therefore a **checkpoint**: run the full test file, confirm green,
  then move on. If the user runs `git init` first, the listed `git` commands
  work as written; otherwise skip the `git` line and keep the checkpoint
  meaning (tests must be green before the next task).
- **Project memory rule (`feedback_no_shortcuts.md`):** no shortcuts, no
  placeholder stubs, bare `except` forbidden (catch specific exceptions),
  type hints + docstrings mandatory, tests must actually pass.
- **Existing tests:** 157 tests currently pass. No task may break them.

---

## File Structure

| File | Responsibility |
|---|---|
| `config.py` (modify) | Validation tunables: refresh cadence, history depth, min trades, tier thresholds |
| `analyzer/signal_validator.py` (create) | Deep-history evaluation → `ValidationSnapshot`. Pure stats helpers + `SignalValidator` engine. |
| `analyzer/state.py` (modify) | `set_validation` / `validation` snapshot slot |
| `analyzer/analysis_loop.py` (modify) | `validation` schedule + off-thread worker |
| `dashboard/serialize.py` (modify) | `serialize_validation` + wire into `snapshot_to_json` |
| `static/app.js` (modify) | Render the confidence block in the DWS panel |
| `static/app.css` (modify) | Styling for the confidence block |
| `tests/test_signal_validator.py` (create) | Unit tests for every pure helper + the engine with a fake connector |

`signal_validator.py` reuses — does not reimplement — `dws_smt.compute_symbol`
and `dws_smt.DwsSmtTrade`. It only adds the statistics layer on top.

---

## Data model (defined in Task 2, referenced everywhere after)

```python
@dataclass(frozen=True)
class SubPeriodStats:
    win_rate: float       # 0..1
    expectancy: float     # net points per trade
    n_trades: int

@dataclass(frozen=True)
class RegimeStats:
    win_rate: float       # 0..1
    expectancy: float     # net points per trade
    n_trades: int

@dataclass(frozen=True)
class ValidationCore:
    n_trades: int
    win_rate: float                 # 0..1
    ci_low: float                   # Wilson 95% lower bound, 0..1
    ci_high: float                  # Wilson 95% upper bound, 0..1
    profit_factor: float            # gross win / gross loss; inf if no losses
    expectancy: float               # net points per trade
    max_drawdown: float             # >= 0, points
    avg_mae: float                  # >= 0, points
    thirds: tuple[SubPeriodStats, SubPeriodStats, SubPeriodStats]
    regime_trend: RegimeStats
    regime_range: RegimeStats
    tier: str                       # "信頼" | "要注意" | "データ不足"

@dataclass(frozen=True)
class ValidationStats:
    symbol: str
    base_tf: str
    raw: ValidationCore
    macro_filtered: ValidationCore  # Plan 1: identical object to `raw`
                                    # (no macro filter exists yet — see spec A.4)

@dataclass(frozen=True)
class ValidationSnapshot:
    generated_at: float             # epoch seconds (UTC)
    compute_ms: float
    by_symbol: dict[str, dict[str, ValidationStats]]   # sym -> base_tf -> stats
```

---

## Task 1: Config constants

**Files:**
- Modify: `config.py` (add a new block after the DWS-SMT block, near line 163)

- [ ] **Step 1: Add the validation config block**

Insert after the `DWS_SMT_BARS` line (`config.py:162`), before the blank lines
preceding `BIAS_TF_WEIGHTS`:

```python

# --------------------------------------------------------------------------- #
# Signal validation layer (precision-optimization spec, Section A)
# --------------------------------------------------------------------------- #
# Deep-history out-of-sample evaluation of the DWS-SMT signal. Runs off-thread
# on its own slow schedule so it never touches the SPEC §19 50 ms budget.
VALIDATION_REFRESH_SEC: Final[float] = 300.0    # re-validate every 5 minutes
VALIDATION_HISTORY_BARS: Final[int] = 5000      # base bars fetched per (sym, TF)
VALIDATION_MIN_TRADES: Final[int] = 30          # below this → tier "データ不足"
# Wilson score interval z for a 95 % two-sided confidence interval.
VALIDATION_CI_Z: Final[float] = 1.96
```

- [ ] **Step 2: Verify the module still imports**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -c "import config; print(config.VALIDATION_HISTORY_BARS, config.VALIDATION_MIN_TRADES)"`
Expected: `5000 30`

- [ ] **Step 3: Commit (checkpoint)**

```bash
git add config.py
git commit -m "feat: add signal-validation config constants"
```

---

## Task 2: Data model + Wilson confidence interval

**Files:**
- Create: `analyzer/signal_validator.py`
- Create: `tests/test_signal_validator.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_signal_validator.py`:

```python
"""Tests for the signal-validation layer."""

from __future__ import annotations

import math

import numpy as np
import pytest

from analyzer import signal_validator as sv


# --------------------------------------------------------------- Wilson interval
def test_wilson_interval_known_value():
    # 60 wins / 100 trials, z=1.96 → Wilson ≈ (0.4974, 0.6960).
    low, high = sv.wilson_interval(60, 100, z=1.96)
    assert low == pytest.approx(0.4974, abs=1e-3)
    assert high == pytest.approx(0.6960, abs=1e-3)
    assert low < high


def test_wilson_interval_zero_trials():
    # No data → the whole [0, 1] band, never a divide-by-zero.
    low, high = sv.wilson_interval(0, 0, z=1.96)
    assert low == 0.0
    assert high == 1.0


def test_wilson_interval_all_wins():
    low, high = sv.wilson_interval(20, 20, z=1.96)
    assert high == pytest.approx(1.0, abs=1e-9)
    assert 0.0 < low < 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest tests/test_signal_validator.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'analyzer.signal_validator'`

- [ ] **Step 3: Create the module with the data model and Wilson helper**

Create `analyzer/signal_validator.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest tests/test_signal_validator.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit (checkpoint)**

```bash
git add analyzer/signal_validator.py tests/test_signal_validator.py
git commit -m "feat: signal_validator data model + Wilson interval"
```

---

## Task 3: Drawdown + trade-summary helpers

These reduce a chronological list of net-point P/Ls to win rate, profit factor,
expectancy and max drawdown.

**Files:**
- Modify: `analyzer/signal_validator.py`
- Modify: `tests/test_signal_validator.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_signal_validator.py`:

```python
# ----------------------------------------------------------------- drawdown
def test_max_drawdown_basic():
    # equity curve: +10, +20, +5(DD 15), +25 → worst peak-to-trough = 15.
    assert sv.max_drawdown([10.0, 10.0, -15.0, 20.0]) == pytest.approx(15.0)


def test_max_drawdown_all_up():
    assert sv.max_drawdown([5.0, 5.0, 5.0]) == pytest.approx(0.0)


def test_max_drawdown_empty():
    assert sv.max_drawdown([]) == pytest.approx(0.0)


# --------------------------------------------------------------- summarize
def test_summarize_pnls_mixed():
    s = sv.summarize_pnls([100.0, -50.0, 100.0, -50.0])
    assert s["n"] == 4
    assert s["win_rate"] == pytest.approx(0.5)
    assert s["profit_factor"] == pytest.approx(2.0)        # 200 / 100
    assert s["expectancy"] == pytest.approx(25.0)          # 100 / 4


def test_summarize_pnls_no_losses():
    s = sv.summarize_pnls([10.0, 20.0])
    assert s["profit_factor"] == math.inf


def test_summarize_pnls_empty():
    s = sv.summarize_pnls([])
    assert s["n"] == 0
    assert s["win_rate"] == 0.0
    assert s["expectancy"] == 0.0
    assert s["profit_factor"] == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest tests/test_signal_validator.py -k "drawdown or summarize" -v`
Expected: FAIL — `AttributeError: module 'analyzer.signal_validator' has no attribute 'max_drawdown'`

- [ ] **Step 3: Add the helpers**

Append to `analyzer/signal_validator.py` (after `wilson_interval`):

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest tests/test_signal_validator.py -v`
Expected: PASS (9 passed)

- [ ] **Step 5: Commit (checkpoint)**

```bash
git add analyzer/signal_validator.py tests/test_signal_validator.py
git commit -m "feat: drawdown + trade-summary helpers"
```

---

## Task 4: Breakeven win rate + tier classification

**Files:**
- Modify: `analyzer/signal_validator.py`
- Modify: `tests/test_signal_validator.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_signal_validator.py`:

```python
# --------------------------------------------------------------- breakeven
def test_breakeven_win_rate_symmetric():
    # avg win == avg loss magnitude → breakeven at 50 %.
    assert sv.breakeven_win_rate([100.0, -100.0, 100.0]) == pytest.approx(0.5)


def test_breakeven_win_rate_no_losses():
    assert sv.breakeven_win_rate([10.0, 20.0]) == pytest.approx(0.0)


def test_breakeven_win_rate_no_wins():
    assert sv.breakeven_win_rate([-10.0, -20.0]) == pytest.approx(1.0)


# -------------------------------------------------------------------- tier
def test_classify_tier_insufficient():
    assert sv.classify_tier(n_trades=10, ci_low=0.9, breakeven=0.5,
                             thirds_expectancy=[1.0, 1.0, 1.0]) == "データ不足"


def test_classify_tier_trusted():
    # enough trades, CI lower bound clears breakeven, all thirds positive.
    assert sv.classify_tier(n_trades=50, ci_low=0.6, breakeven=0.5,
                            thirds_expectancy=[1.0, 2.0, 0.5]) == "信頼"


def test_classify_tier_caution_unstable():
    # enough trades but one third has negative expectancy.
    assert sv.classify_tier(n_trades=50, ci_low=0.6, breakeven=0.5,
                            thirds_expectancy=[1.0, -2.0, 0.5]) == "要注意"


def test_classify_tier_caution_ci_below_breakeven():
    assert sv.classify_tier(n_trades=50, ci_low=0.45, breakeven=0.5,
                            thirds_expectancy=[1.0, 1.0, 1.0]) == "要注意"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest tests/test_signal_validator.py -k "breakeven or tier" -v`
Expected: FAIL — `AttributeError: ... has no attribute 'breakeven_win_rate'`

- [ ] **Step 3: Add the helpers**

Append to `analyzer/signal_validator.py` (after `summarize_pnls`):

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest tests/test_signal_validator.py -v`
Expected: PASS (16 passed)

- [ ] **Step 5: Commit (checkpoint)**

```bash
git add analyzer/signal_validator.py tests/test_signal_validator.py
git commit -m "feat: breakeven win rate + tier classification"
```

---

## Task 5: Reduce a trade list to a ValidationCore

`evaluate_trades` is the heart of the layer: it takes the closed trades of one
DWS window plus the aligned per-bar spread and ADX arrays, and produces a
`ValidationCore`.

**Trade economics:** `DwsSmtTrade.points` is a signed *price* move. Net points =
`points / point_size − spread_at_entry`, where `point_size` is the broker
`point` and the `spread` column is already in points. MAE is `mae / point_size`.

**Regime:** a trade is "trend" when ADX at its entry bar is
`>= config.BIAS_REGIME_ADX_HIGH` (25), else "range".

**Files:**
- Modify: `analyzer/signal_validator.py`
- Modify: `tests/test_signal_validator.py`

- [ ] **Step 1: Add the `DwsSmtTrade` import, then write the failing test**

First, add `DwsSmtTrade` to the imports at the top of
`tests/test_signal_validator.py` — change the existing
`from analyzer import signal_validator as sv` line so the import block reads:

```python
from analyzer import signal_validator as sv
from analyzer.dws_smt import DwsSmtTrade
```

Then append to `tests/test_signal_validator.py`:

```python
# --------------------------------------------------------------- evaluate
def _trade(entry_idx, direction, points, mae=0.0, is_open=False):
    return DwsSmtTrade(entry_idx=entry_idx, direction=direction,
                       points=points, mae=mae, is_open=is_open)


def test_evaluate_trades_skips_open_trade():
    # 1 closed winner + 1 open trade → only the closed one counts.
    trades = (_trade(0, 1, 1.0), _trade(2, 1, 5.0, is_open=True))
    spread = np.zeros(4)
    adx = np.full(4, 30.0)
    core = sv.evaluate_trades(trades, spread_pts=spread, adx=adx, point=1.0)
    assert core.n_trades == 1


def test_evaluate_trades_cost_is_deducted():
    # raw +10 price points, point=1.0, spread 3 pts at entry → net 7.
    trades = (_trade(0, 1, 10.0),)
    spread = np.array([3.0, 0.0])
    adx = np.array([30.0, 30.0])
    core = sv.evaluate_trades(trades, spread_pts=spread, adx=adx, point=1.0)
    assert core.expectancy == pytest.approx(7.0)


def test_evaluate_trades_regime_split():
    # entry 0 in a trend bar (ADX 30), entry 1 in a range bar (ADX 10).
    trades = (_trade(0, 1, 10.0), _trade(1, 1, -4.0))
    spread = np.zeros(2)
    adx = np.array([30.0, 10.0])
    core = sv.evaluate_trades(trades, spread_pts=spread, adx=adx, point=1.0)
    assert core.regime_trend.n_trades == 1
    assert core.regime_range.n_trades == 1
    assert core.regime_trend.expectancy == pytest.approx(10.0)
    assert core.regime_range.expectancy == pytest.approx(-4.0)


def test_evaluate_trades_empty_is_insufficient():
    core = sv.evaluate_trades((), spread_pts=np.zeros(1),
                              adx=np.zeros(1), point=1.0)
    assert core.n_trades == 0
    assert core.tier == "データ不足"


def test_evaluate_trades_thirds_split():
    # 30 identical winners → all three thirds have 10 trades, all positive.
    trades = tuple(_trade(i, 1, 2.0) for i in range(30))
    spread = np.zeros(31)
    adx = np.full(31, 30.0)
    core = sv.evaluate_trades(trades, spread_pts=spread, adx=adx, point=1.0)
    assert [t.n_trades for t in core.thirds] == [10, 10, 10]
    assert core.tier == "信頼"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest tests/test_signal_validator.py -k evaluate -v`
Expected: FAIL — `AttributeError: ... has no attribute 'evaluate_trades'`

- [ ] **Step 3: Implement `evaluate_trades`**

Append to `analyzer/signal_validator.py` (after `classify_tier`):

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest tests/test_signal_validator.py -v`
Expected: PASS (21 passed)

- [ ] **Step 5: Commit (checkpoint)**

```bash
git add analyzer/signal_validator.py tests/test_signal_validator.py
git commit -m "feat: evaluate_trades — trade list to ValidationCore"
```

---

## Task 6: `SignalValidator` engine — deep-history fetch + pass

The engine fetches deep history through the existing parallel fetcher (building
`TimeframeSpec`s with `bars_to_fetch = VALIDATION_HISTORY_BARS`), runs
`dws_smt.compute_symbol` over that deep window, computes the base-TF ADX, and
calls `evaluate_trades`.

**Window alignment:** `dws_smt.compute_symbol(..., out_bars=VALIDATION_HISTORY_BARS)`
emits the trailing `out_bars` of the base history; `_build_window` slices the
base frame from `start = max(0, len(base_df) - out_bars)`. The engine slices
the `spread` column and the ADX series with the *same* `start` so trade
`entry_idx` lines up.

**Files:**
- Modify: `analyzer/signal_validator.py`
- Modify: `tests/test_signal_validator.py`

- [ ] **Step 1: Add the pandas import, then write the failing test**

First, add `import pandas as pd` to the imports at the top of
`tests/test_signal_validator.py` (the engine test builds DataFrames):

```python
import numpy as np
import pandas as pd
import pytest
```

Then append to `tests/test_signal_validator.py`:

```python
# ----------------------------------------------------------------- engine
class _FakeConnector:
    """Minimal connector stand-in: serves pre-built deep-history frames."""

    def __init__(self, frames_by_pair):
        # frames_by_pair: {(base, tf_label): DataFrame}
        self._frames = frames_by_pair

    def fetch_rates_parallel(self, bases, timeframes):
        out = {}
        for b in bases:
            for tf in timeframes:
                df = self._frames.get((b, tf.label))
                if df is not None:
                    out[(b, tf.label)] = df
        return out


def _ramp_frame(n: int, start: float = 100.0, step: float = 1.0):
    """A steadily rising OHLC frame — DWS-SMT goes all-green on it."""
    idx = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")
    close = start + step * np.arange(n, dtype=float)
    return pd.DataFrame(
        {"open": close, "high": close + 0.5, "low": close - 0.5,
         "close": close, "tick_volume": np.ones(n), "spread": np.full(n, 2.0),
         "real_volume": np.zeros(n)},
        index=idx,
    )


def test_signal_validator_compute_builds_snapshot():
    n = 600
    frames = {}
    for tf in ("M15", "H1", "H4", "D1", "W1"):
        frames[("EURUSD", tf)] = _ramp_frame(n)
    conn = _FakeConnector(frames)
    validator = sv.SignalValidator(conn, history_bars=n)
    snap = validator.compute(["EURUSD"], broker_meta={"EURUSD": {"point": 0.0001}})

    assert isinstance(snap, sv.ValidationSnapshot)
    assert "EURUSD" in snap.by_symbol
    # M15 base produces a window → a ValidationStats entry exists.
    assert "M15" in snap.by_symbol["EURUSD"]
    stats = snap.by_symbol["EURUSD"]["M15"]
    assert stats.symbol == "EURUSD"
    assert stats.base_tf == "M15"
    # Plan 1: macro_filtered mirrors raw exactly.
    assert stats.macro_filtered is stats.raw


def test_signal_validator_compute_handles_missing_symbol():
    conn = _FakeConnector({})           # no frames at all
    validator = sv.SignalValidator(conn, history_bars=100)
    snap = validator.compute(["EURUSD"], broker_meta={})
    # No data → the symbol simply has no base-TF entries, no crash.
    assert snap.by_symbol.get("EURUSD", {}) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest tests/test_signal_validator.py -k signal_validator -v`
Expected: FAIL — `AttributeError: ... has no attribute 'SignalValidator'`

- [ ] **Step 3: Implement the engine**

Append to `analyzer/signal_validator.py` (after `_split_three`):

```python
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
    ) -> None:
        self._connector = connector
        self._history_bars = history_bars
        # Deep-history fetch specs: same MT5 constants, far deeper bar counts.
        self._deep_specs = tuple(
            config.TimeframeSpec(label, _TF_CONST[label], 0, history_bars)
            for label in _NEEDED_TFS
            if label in _TF_CONST
        )

    def compute(
        self,
        bases: list[str],
        broker_meta: dict[str, dict[str, float]],
    ) -> ValidationSnapshot:
        """Run one validation pass over *bases*.

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
        rates = self._connector.fetch_rates_parallel(bases, self._deep_specs)
        by_symbol: dict[str, dict[str, ValidationStats]] = {}

        for base in bases:
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

        return evaluate_trades(window.trades, spread_pts=spread_pts,
                               adx=adx, point=point)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest tests/test_signal_validator.py -v`
Expected: PASS (23 passed)

- [ ] **Step 5: Run the whole suite — nothing regressed**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest -q`
Expected: PASS (180 passed — 157 existing + 23 new)

- [ ] **Step 6: Commit (checkpoint)**

```bash
git add analyzer/signal_validator.py tests/test_signal_validator.py
git commit -m "feat: SignalValidator engine — deep-history validation pass"
```

---

## Task 7: Wire `ValidationSnapshot` into `LatestState`

**Files:**
- Modify: `analyzer/state.py`
- Modify: `tests/test_state_and_serialize.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_state_and_serialize.py`:

```python
def test_state_set_and_read_validation():
    from analyzer.state import LatestState
    from analyzer.signal_validator import ValidationSnapshot

    st = LatestState()
    assert st.validation is None
    before = st.analysis_version
    snap = ValidationSnapshot(generated_at=1.0, compute_ms=2.0, by_symbol={})
    st.set_validation(snap)
    assert st.validation is snap
    # Validation is a heavy domain → it bumps analysis_version.
    assert st.analysis_version == before + 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest tests/test_state_and_serialize.py::test_state_set_and_read_validation -v`
Expected: FAIL — `AttributeError: 'LatestState' object has no attribute 'validation'`

- [ ] **Step 3: Add the state slot**

In `analyzer/state.py`:

1. Add the import near the other analyzer imports (after the `calendar_feed`
   import, `state.py:21`):

```python
from analyzer.signal_validator import ValidationSnapshot
```

2. Add the field in `__init__` after `self._calendar` (`state.py:88`):

```python
        self._validation: Optional[ValidationSnapshot] = None
```

3. Add the writer after `set_calendar` (`state.py:169`):

```python
    def set_validation(self, snapshot: ValidationSnapshot) -> None:
        with self._cond:
            self._validation = snapshot
            self._monotonic_version += 1
            self._analysis_version += 1
            self._cond.notify_all()
```

4. Add the reader after the `calendar` property (`state.py:226`):

```python
    @property
    def validation(self) -> ValidationSnapshot | None:
        with self._lock:
            return self._validation
```

5. Add `validation` to the `snapshot()` dict (inside the returned dict in
   `snapshot()`, after the `"calendar"` line):

```python
                "validation": self._validation,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest tests/test_state_and_serialize.py::test_state_set_and_read_validation -v`
Expected: PASS

- [ ] **Step 5: Commit (checkpoint)**

```bash
git add analyzer/state.py tests/test_state_and_serialize.py
git commit -m "feat: LatestState validation snapshot slot"
```

---

## Task 8: `validation` schedule + off-thread worker

Mirror the calendar pattern exactly: an in-flight `threading.Event` guard plus
a daemon worker so the deep-history fetch never blocks the 0.5 s price tick.

**Files:**
- Modify: `analyzer/analysis_loop.py`

- [ ] **Step 1: Add the import**

After `from analyzer.line_reader import ...` (`analysis_loop.py:44`), add:

```python
from analyzer.signal_validator import SignalValidator
```

- [ ] **Step 2: Add constructor parameters and state**

In `AnalysisLoop.__init__`, add a parameter after `calendar_engine` in the
signature:

```python
        signal_validator: SignalValidator | None = None,
```

and after `calendar_interval` in the signature:

```python
        validation_interval: float = config.VALIDATION_REFRESH_SEC,
```

In the constructor body, after `self._calendar_engine = ...` (`analysis_loop.py:96`):

```python
        self._signal_validator = signal_validator or SignalValidator(connector)
        # Deep-history validation runs off-thread — the parallel fetch of
        # VALIDATION_HISTORY_BARS across every symbol/TF takes far longer than
        # the 0.5 s price tick may wait.
        self._validation_inflight = threading.Event()
```

In `self._schedules`, add a fifth entry after `_Schedule("calendar", ...)`:

```python
            _Schedule("validation", validation_interval),
```

- [ ] **Step 3: Register the dispatch handler**

In `_dispatch`, add to the `handler` dict after the `"calendar"` line:

```python
            "validation": self._do_validation_refresh,
```

- [ ] **Step 4: Add the handler + worker**

After `_calendar_refresh_worker` (`analysis_loop.py:326`), add:

```python
    def _do_validation_refresh(self, bases: list[str]) -> None:
        """Spec Section A: deep-history signal validation every 5 minutes.

        Dispatched to a daemon worker — the parallel deep-history fetch is far
        too slow to run inside the loop without starving the 0.5 s price tick.
        At most one validation runs at a time; overlapping cycles are skipped.
        """
        if self._validation_inflight.is_set():
            log.debug("validation: previous pass still in flight, skipping tick")
            return
        self._validation_inflight.set()
        worker = threading.Thread(
            target=self._validation_refresh_worker,
            args=(list(bases),),
            name="signal-validation", daemon=True,
        )
        worker.start()

    def _validation_refresh_worker(self, bases: list[str]) -> None:
        try:
            snap = self._signal_validator.compute(bases, self._state.broker_meta)
            self._state.set_validation(snap)
        except Exception:               # noqa: BLE001 — never reach the loop
            log.exception("signal-validation worker failed")
        finally:
            self._validation_inflight.clear()
```

- [ ] **Step 5: Run the suite — analysis-loop tests still pass**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest tests/ -q`
Expected: PASS (181 passed — the new state test + 180 from before)

- [ ] **Step 6: Commit (checkpoint)**

```bash
git add analyzer/analysis_loop.py
git commit -m "feat: off-thread validation schedule in the analysis loop"
```

---

## Task 9: Serialize `ValidationSnapshot` for the wire

**Files:**
- Modify: `dashboard/serialize.py`
- Modify: `tests/test_state_and_serialize.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_state_and_serialize.py`:

```python
def test_serialize_validation_shape():
    from dashboard.serialize import serialize_validation
    from analyzer.signal_validator import (
        RegimeStats, SubPeriodStats, ValidationCore, ValidationStats,
        ValidationSnapshot,
    )

    third = SubPeriodStats(win_rate=0.6, expectancy=1.5, n_trades=10)
    regime = RegimeStats(win_rate=0.5, expectancy=0.5, n_trades=5)
    core = ValidationCore(
        n_trades=30, win_rate=0.6, ci_low=0.45, ci_high=0.73,
        profit_factor=1.8, expectancy=1.2, max_drawdown=8.0, avg_mae=3.0,
        thirds=(third, third, third), regime_trend=regime, regime_range=regime,
        tier="信頼",
    )
    stats = ValidationStats(symbol="EURUSD", base_tf="M15",
                            raw=core, macro_filtered=core)
    snap = ValidationSnapshot(generated_at=1.0, compute_ms=2.0,
                              by_symbol={"EURUSD": {"M15": stats}})

    out = serialize_validation(snap)
    assert out["by_symbol"]["EURUSD"]["M15"]["raw"]["tier"] == "信頼"
    assert out["by_symbol"]["EURUSD"]["M15"]["raw"]["n_trades"] == 30
    assert len(out["by_symbol"]["EURUSD"]["M15"]["raw"]["thirds"]) == 3
    assert serialize_validation(None) is None


def test_serialize_validation_handles_infinite_pf():
    from dashboard.serialize import serialize_validation
    from analyzer.signal_validator import (
        RegimeStats, SubPeriodStats, ValidationCore, ValidationStats,
        ValidationSnapshot,
    )
    third = SubPeriodStats(win_rate=1.0, expectancy=2.0, n_trades=10)
    regime = RegimeStats(win_rate=1.0, expectancy=2.0, n_trades=10)
    core = ValidationCore(
        n_trades=30, win_rate=1.0, ci_low=0.9, ci_high=1.0,
        profit_factor=float("inf"), expectancy=2.0, max_drawdown=0.0,
        avg_mae=0.0, thirds=(third, third, third),
        regime_trend=regime, regime_range=regime, tier="信頼",
    )
    stats = ValidationStats(symbol="EURUSD", base_tf="M15",
                            raw=core, macro_filtered=core)
    snap = ValidationSnapshot(generated_at=1.0, compute_ms=2.0,
                              by_symbol={"EURUSD": {"M15": stats}})
    # inf must serialise to null — json.dumps would otherwise raise.
    out = serialize_validation(snap)
    assert out["by_symbol"]["EURUSD"]["M15"]["raw"]["profit_factor"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest tests/test_state_and_serialize.py -k validation -v`
Expected: FAIL — `ImportError: cannot import name 'serialize_validation'`

- [ ] **Step 3: Add the serializer**

In `dashboard/serialize.py`:

1. Extend the `signal_validator` import — add it after the `dws_smt` import
   (`serialize.py:28`):

```python
from analyzer.signal_validator import (
    RegimeStats,
    SubPeriodStats,
    ValidationCore,
    ValidationSnapshot,
    ValidationStats,
)
```

2. Add the serializer functions after `serialize_calendar`
   (`serialize.py:436`):

```python
# --------------------------------------------------------------------------- #
# Signal validation layer (precision-optimization spec, Section A)
# --------------------------------------------------------------------------- #

def _serialize_sub_period(s: SubPeriodStats) -> dict[str, Any]:
    return {
        "win_rate": _opt_float(s.win_rate),
        "expectancy": _opt_float(s.expectancy),
        "n_trades": int(s.n_trades),
    }


def _serialize_regime(s: RegimeStats) -> dict[str, Any]:
    return {
        "win_rate": _opt_float(s.win_rate),
        "expectancy": _opt_float(s.expectancy),
        "n_trades": int(s.n_trades),
    }


def serialize_validation_core(c: ValidationCore) -> dict[str, Any]:
    """Serialise one :class:`ValidationCore`.

    ``profit_factor`` may be ``inf`` (no losing trades); ``_opt_float`` maps
    that to ``null`` so ``json.dumps`` never raises.
    """
    return {
        "n_trades": int(c.n_trades),
        "win_rate": _opt_float(c.win_rate),
        "ci_low": _opt_float(c.ci_low),
        "ci_high": _opt_float(c.ci_high),
        "profit_factor": _opt_float(c.profit_factor),
        "expectancy": _opt_float(c.expectancy),
        "max_drawdown": _opt_float(c.max_drawdown),
        "avg_mae": _opt_float(c.avg_mae),
        "thirds": [_serialize_sub_period(t) for t in c.thirds],
        "regime_trend": _serialize_regime(c.regime_trend),
        "regime_range": _serialize_regime(c.regime_range),
        "tier": c.tier,
    }


def serialize_validation_stats(s: ValidationStats) -> dict[str, Any]:
    return {
        "symbol": s.symbol,
        "base_tf": s.base_tf,
        "raw": serialize_validation_core(s.raw),
        "macro_filtered": serialize_validation_core(s.macro_filtered),
    }


def serialize_validation(s: ValidationSnapshot | None) -> dict[str, Any] | None:
    """Serialise the whole validation snapshot for the WebSocket payload."""
    if s is None:
        return None
    return {
        "generated_at": float(s.generated_at),
        "compute_ms": float(s.compute_ms),
        "min_trades": int(config.VALIDATION_MIN_TRADES),
        "by_symbol": {
            sym: {tf: serialize_validation_stats(st) for tf, st in per_tf.items()}
            for sym, per_tf in s.by_symbol.items()
        },
    }
```

3. Wire it into `snapshot_to_json` — add after the `"calendar"` line
   (`serialize.py:488`):

```python
        "validation": serialize_validation(snap["validation"]),  # type: ignore[arg-type]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest tests/test_state_and_serialize.py -k validation -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Run the whole suite**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest -q`
Expected: PASS (183 passed)

- [ ] **Step 6: Commit (checkpoint)**

```bash
git add dashboard/serialize.py tests/test_state_and_serialize.py
git commit -m "feat: serialize ValidationSnapshot for the WS payload"
```

---

## Task 10: Render the confidence block in the DWS panel

The DWS panel skeleton (`ensureDwsSkeleton`, `app.js:1123`) already has a
`dws-stats` line. Add a `dws-validation` line below it that shows the confidence
tier and metrics for the currently selected base TF (`UI.dwsBase`).

**Files:**
- Modify: `static/app.js`
- Modify: `static/app.css`

- [ ] **Step 1: Add the skeleton element**

In `ensureDwsSkeleton` (`app.js:1123`), add a line after the `dws-stats` div
(`app.js:1137`):

```javascript
        <div class="dws-validation" data-bind="dws-validation-${sym}">--</div>
```

- [ ] **Step 2: Add the validation-render helper**

Add this function in `static/app.js` immediately after `updateDwsStats`
(after `app.js:1368`):

```javascript
/** Render the out-of-sample confidence block for the selected base TF.
 *  Data comes from the validation layer (snap.validation); it refreshes on
 *  its own 5-minute cadence so it is often older than the live histogram. */
function updateDwsValidation(sym, snap) {
    const el = $bind('dws-validation-' + sym);
    if (!el) return;
    const v = snap.validation;
    const stats = v && v.by_symbol && v.by_symbol[sym]
                  && v.by_symbol[sym][UI.dwsBase];
    if (!stats || !stats.raw) {
        el.className = 'dws-validation';
        el.textContent = '検証 — データ未取得';
        return;
    }
    const c = stats.raw;
    const tierCls = c.tier === '信頼' ? 'trusted'
                  : c.tier === '要注意' ? 'caution' : 'insufficient';
    el.className = 'dws-validation ' + tierCls;

    const pct = x => (x == null ? '--' : Math.round(x * 100) + '%');
    const pf = c.profit_factor == null ? '∞'
             : c.profit_factor.toFixed(2);
    const exp = c.expectancy == null ? '--' : Math.round(c.expectancy);
    const expCls = c.expectancy > 0 ? 'pos' : c.expectancy < 0 ? 'neg' : '';
    const ci = `${pct(c.ci_low)}–${pct(c.ci_high)}`;
    const thirds = (c.thirds || [])
        .map(t => (t.expectancy > 0 ? '✓' : '✗')).join('');

    el.innerHTML =
        `<span class="dws-vtier">${esc(c.tier)}</span> `
      + `<span class="dws-vmeta">OOS検証 N=${c.n_trades} · `
      + `勝率 ${pct(c.win_rate)} (95%CI ${ci}) · `
      + `PF ${esc(pf)} · 期待値 <span class="dws-num ${expCls}">`
      + `${exp >= 0 ? '+' : ''}${exp}</span>pt · `
      + `安定性 ${esc(thirds)}</span>`;
}
```

- [ ] **Step 3: Call the helper from `drawDwsCanvas`**

In `drawDwsCanvas`, find where `updateDwsStats` is called (search for
`updateDwsStats(sym`) and add a call to `updateDwsValidation` right after it.
There are TWO places `dws-stats` is touched — the data-present path and the
"データなし" early-return path. Cover both:

In the "データなし" early-return branch (`app.js:1395`), after the
`statsEl0` block, add:

```javascript
        updateDwsValidation(sym, snap);
```

And in the data-present path, immediately after the existing
`updateDwsStats(sym, snap, win);` call, add:

```javascript
    updateDwsValidation(sym, snap);
```

- [ ] **Step 4: Bust the DWS stamp on a validation update**

The `paintDws` painter is gated by `changed('dws', analysis.generated_at)`
(`app.js:1171`). A validation refresh does NOT change `analysis.generated_at`,
so the new block would not repaint. Fix `paintDws` to also key on the
validation timestamp. Replace the body of `paintDws` (`app.js:1166-1176`)
with:

```javascript
function paintDws(snap) {
    // Skip on price-only frames — the histogram is analysis-derived. The
    // 'dws' stamp is busted on panel expand/collapse and on a TF-pill click
    // so those still force a redraw. The validation layer refreshes on its
    // own cadence, so its timestamp is folded into the stamp key.
    const analysis = snap.analysis;
    const vts = (snap.validation && snap.validation.generated_at) || 0;
    if (analysis && !changed('dws', analysis.generated_at + ':' + vts)) return;
    for (const sym of SYMBOL_ORDER) {
        ensureDwsSkeleton(sym);
        drawDwsCanvas(snap, sym);
    }
}
```

- [ ] **Step 5: Add the CSS**

Append to `static/app.css`:

```css
/* DWS-SMT signal-validation confidence block */
.dws-validation {
    font-size: 11px;
    line-height: 1.5;
    padding: 3px 6px;
    margin-top: 2px;
    border-radius: 3px;
    color: #c5ccd8;
    background: rgba(255, 255, 255, 0.03);
}
.dws-validation .dws-vtier {
    font-weight: 700;
    padding: 0 5px;
    border-radius: 3px;
}
.dws-validation.trusted      .dws-vtier { background: #1f6f54; color: #eafff7; }
.dws-validation.caution      .dws-vtier { background: #7a5a1e; color: #fff3da; }
.dws-validation.insufficient .dws-vtier { background: #4a4f5c; color: #c5ccd8; }
.dws-validation .dws-vmeta { color: #8089a0; }
```

- [ ] **Step 6: Verify in the browser**

Start the server:
`C:\Users\ohuch\AppData\Local\Python\bin\python.exe main.py`
(or restart it if already running).

With the browse skill (`B=~/.claude/skills/gstack/browse/dist/browse`):

```bash
$B goto http://127.0.0.1:8050
# wait ~6s for the WS snapshot
$B click '[data-symbol="XAUUSD"]'
$B screenshot /tmp/dws_validation.png
$B console
```

Expected: the expanded XAUUSD panel shows a `dws-validation` line. Until the
first 5-minute validation pass completes it reads "検証 — データ未取得"; after
that it shows a tier badge + metrics. No console errors.

Note: the validation worker fires on its first scheduled tick. To see real
data without waiting 5 minutes, the validation schedule's `next_run` starts at
0 (due immediately) — the first pass runs at startup, so data should appear
within a few seconds of the deep-history fetch completing.

- [ ] **Step 7: Commit (checkpoint)**

```bash
git add static/app.js static/app.css
git commit -m "feat: render OOS confidence block in the DWS panel"
```

---

## Task 11: Full verification

- [ ] **Step 1: Run the entire test suite**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest -q`
Expected: PASS (183 passed, 0 failed)

- [ ] **Step 2: Restart the server and confirm a clean boot**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe main.py`
Expected: log shows "Analysis loop started", no exceptions, and within a
minute a "signal-validation" worker log line (or no error from it).

- [ ] **Step 3: Browser smoke test**

With the browse skill, load `http://127.0.0.1:8050`, expand two symbols, and
confirm:
- the `dws-validation` line renders for the selected base TF;
- switching the 4H/1H/M15 base pill updates the validation block;
- `$B console` shows no errors.

- [ ] **Step 4: Confirm the SPEC §19 budget is intact**

The validation pass runs off-thread; it must not inflate `compute_ms`. In the
browser console check `latestSnap.analysis.compute_ms` stays under 50. The
header "CYCLE" readout should be unchanged from before this plan.

- [ ] **Step 5: Final commit (checkpoint)**

```bash
git add -A
git commit -m "feat: signal validation layer complete — OOS confidence metrics"
```

---

## Self-review notes (for the implementer)

- **Spec coverage:** Section A.2 metrics — N, Wilson CI, PF, expectancy, maxDD,
  avg MAE (Task 5); thirds stability (Task 5); regime split (Task 5); tier
  (Task 4). A.4 raw vs macro_filtered — both present, identical in Plan 1
  (Task 2 model + Task 6 engine). A.6 cadence — Task 1 + Task 8. A.7 files —
  all covered. A.8 error handling — `_validate_symbol` wrapped, worker wrapped
  (Tasks 6, 8). A.9 tests — Tasks 2-6, 9.
- **macro_filtered** is intentionally the same object as `raw` here; the macro
  layer plan (`2026-05-22-macro-layer.md`, written after this plan executes)
  changes how `macro_filtered` triggers are selected.
- **No deep-history fetch on the price path** — the worker is daemon + in-flight
  guarded, identical to the calendar pattern.
