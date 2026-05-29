"""XAUUSD 16-year deep-history OOS validation — strict statistics, no filter.

Loads ALL XAUUSD Dukascopy CSV bars (W1 from 2009-12-28, D1/H4/H1/M15 from
2010-01-01, all through 2025-12-31), runs the production deterministic
DWS-SMT signal end-to-end, and reduces the full per-trade ledger to a
statistically rigorous OOS report:

    1. Aggregate (all-period)
       - N, win rate, Wilson 95% CI (normal binomial-independent)
       - Profit factor, expectancy, max drawdown, breakeven WR
       - Tier (信頼 / 要注意 / データ不足)

    2. Year-by-year breakdown
       - Per calendar year: N, WR, expectancy, PF, DD
       - Regime stability check (which years carry the edge / regress)

    3. Chronological 2-period split (2010-2017 vs 2018-2025)
       - Per-period stats
       - 2-proportion z-test on WR drift
       - 2-sided z-test on expectancy drift
       - Drift verdict: STABLE / DRIFT / REGIME-CHANGE

    4. Moving-block bootstrap CI on WR
       - 10,000 iterations, block size = 50 trades
       - Honest CI in the presence of trade-to-trade autocorrelation
       - Compared against Wilson CI to show how much over-confident the
         Bernoulli-independent CI is

    5. Multiple-testing correction
       - Bonferroni α / (3 base TFs) = 0.0167 — period-drift significance
         is reported both raw and Bonferroni-corrected

NO YEAR FILTER. NO WARMUP SKIP. Indicator NaN drop is the only natural
attenuation — handled inside ``dws_smt.compute_symbol`` and the trade
pairing logic (the earliest bars whose ADX/EMA/RSI/ATR have not warmed up
do not produce trades). All 16 years are evaluated identically.

Output:
    data/oos_xauusd_16y.json     -- machine-readable, dashboard-consumed
    data/oos_xauusd_16y.md       -- human-readable report

Run from project root::

    py scripts/_oos_xauusd_16y.py
"""

from __future__ import annotations

import json
import logging
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from analyzer import dws_smt  # noqa: E402
from analyzer.signal_validator import (  # noqa: E402
    evaluate_trades, wilson_interval, summarize_pnls,
    breakeven_win_rate, classify_tier, max_drawdown,
)

# Per-symbol point sizes for all 8 currently-active SYMBOLS in config.
# Mirrors POINT_BY_SYMBOL in _backtest_all_yearly_swap.py — kept inline so
# this script is self-contained.
POINT_BY_SYMBOL: dict[str, float] = {
    "XAUUSD": 0.001,
    "USDJPY": 0.001, "EURJPY": 0.001, "GBPJPY": 0.001, "AUDJPY": 0.001,
    "EURUSD": 0.00001, "GBPUSD": 0.00001, "AUDUSD": 0.00001,
}

_TF_FILENAMES: dict[str, str] = {
    "W1": "Weekly", "D1": "Daily", "H4": "4 Hours",
    "H1": "Hourly", "M15": "15 Mins",
}
_DATE_RANGE: dict[str, str] = {
    "W1":  "2009.12.28_2025.12.29",
    "D1":  "2010.01.01_2025.12.31",
    "H4":  "2010.01.01_2025.12.31",
    "H1":  "2010.01.01_2025.12.31",
    "M15": "2010.01.01_2025.12.31",
}


def _load_csv(symbol: str, tf: str, side: str) -> pd.DataFrame:
    """Load one Dukascopy CSV (Bid or Ask), indexed by true UTC.

    Dukascopy "EET" timestamps OBSERVE EU daylight saving (EET=UTC+2 winter,
    EEST=UTC+3 summer) — verified empirically from the daily maintenance gap
    sitting at a constant EET wall-clock year-round. A fixed -2h therefore left
    every summer bar +1h late, smearing the hour-of-day win-rate by 1h for ~half
    the history. Localizing to a DST-aware EET zone and converting to UTC fixes
    the hour buckets in both seasons. (Whole-hour relabel only — bar OHLC, the
    DWS-SMT triggers and all P/L are unchanged; only timestamps/hours move.)
    """
    fname = f"{symbol}_{_TF_FILENAMES[tf]}_{side}_{_DATE_RANGE[tf]}.csv"
    path = PROJECT_ROOT / fname
    if not path.exists():
        raise FileNotFoundError(path)
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={
        "Time (EET)": "time", "Open": "open", "High": "high",
        "Low": "low", "Close": "close", "Volume": "tick_volume",
    })
    naive = pd.to_datetime(df["time"], format="%Y.%m.%d %H:%M:%S")
    df["time"] = (
        naive.dt.tz_localize("Europe/Bucharest",
                             ambiguous=True, nonexistent="shift_forward")
             .dt.tz_convert("UTC").dt.tz_localize(None)
    )
    return df.set_index("time").sort_index()


def _load_tf(symbol: str, tf: str, point: float) -> pd.DataFrame:
    """Load one TF as MT5-compatible OHLC + spread + zero real_volume."""
    bid = _load_csv(symbol, tf, "Bid")
    ask = _load_csv(symbol, tf, "Ask")
    common = bid.index.intersection(ask.index)
    bid, ask = bid.loc[common], ask.loc[common]
    spread_pts = ((ask["close"] - bid["close"]) / point).round().clip(lower=0)
    out = bid.copy()
    out["spread"] = spread_pts.astype("int64")
    out["real_volume"] = 0
    return out


log = logging.getLogger("oos_16y_full")

OUT_DIR = PROJECT_ROOT / "data"
JSON_OUT = OUT_DIR / "oos_xauusd_16y.json"   # legacy name kept (now multi-symbol)
MD_OUT = OUT_DIR / "oos_xauusd_16y.md"

# Bootstrap parameters — block bootstrap respects trade autocorrelation.
BOOTSTRAP_ITER = 10_000
BOOTSTRAP_BLOCK = 50           # trades per block; > typical autocorrelation lag
BOOTSTRAP_SEED = 20260527      # deterministic across runs

# Chronological period split — fixed boundary, both halves are equally
# evaluated (no training happens on either side; DWS-SMT is parameter-free).
PERIOD_SPLIT_YEAR = 2018       # 2010-2017 vs 2018-2025

# Base TFs validated (matches config.DWS_SMT_STACKS).
BASE_TFS = ("M15", "H1", "H4")

# Bonferroni: 3 base TFs simultaneously tested → α / 3.
BONFERRONI_K = len(BASE_TFS)
ALPHA = 0.05
BONFERRONI_ALPHA = ALPHA / BONFERRONI_K


# --------------------------------------------------------------------------- #
# Per-trade ledger build
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TradeRow:
    """One closed trade reduced to the columns we need for OOS stats."""
    entry_year: int
    entry_hour_jst: int      # 0-23, Asia/Tokyo — for the hourly win-rate heatmap
    entry_ms: int            # epoch ms (UTC) — for the trigger-history table
    direction: int
    net_pts: float
    mae_pts: float
    bar_adx: float


def _build_trade_rows(
    window: dws_smt.DwsSmtWindow,
    base_df: pd.DataFrame,
    point: float,
) -> list[TradeRow]:
    """Slice every closed trade in *window* to a flat ``TradeRow`` list.

    Mirrors the index alignment ``SignalValidator._evaluate_window`` does:
    spread_pts / adx are sliced to the same emit-window offset so the trade
    ``entry_idx`` lines up with both arrays.
    """
    from analyzer import indicators
    n_bars = len(base_df)
    emitted = window.times_ms.size
    start = max(0, n_bars - emitted)
    spread_pts = base_df["spread"].to_numpy(dtype=np.float64)[start:] \
        if "spread" in base_df.columns else np.zeros(emitted, dtype=np.float64)
    high = base_df["high"].to_numpy(dtype=np.float64)[None, :]
    low = base_df["low"].to_numpy(dtype=np.float64)[None, :]
    close = base_df["close"].to_numpy(dtype=np.float64)[None, :]
    adx_2d, _, _ = indicators.adx(high, low, close, config.ADX_PERIOD)
    adx = np.nan_to_num(adx_2d[0][start:], nan=0.0)

    times_ns = window.times_ms.astype("int64") * 1_000_000
    rows: list[TradeRow] = []
    for t in window.trades:
        if t.is_open:
            continue
        ei = t.entry_idx
        if ei >= times_ns.size or ei >= spread_pts.size:
            continue
        cost = float(spread_pts[ei])
        net = t.points / point - cost
        ts = pd.Timestamp(int(times_ns[ei]), unit="ns", tz="UTC")
        ts_jst = ts.tz_convert("Asia/Tokyo")
        rows.append(TradeRow(
            entry_year=int(ts_jst.year),
            entry_hour_jst=int(ts_jst.hour),
            entry_ms=int(times_ns[ei] // 1_000_000),
            direction=int(t.direction),
            net_pts=float(net),
            mae_pts=float(t.mae) / point,
            bar_adx=float(adx[ei]) if ei < adx.size else 0.0,
        ))
    return rows


# Per-year recent-trade cap shipped to the dashboard (newest first). Older
# triggers still count toward that year's SUMMARY (computed over the full year).
TRIGGER_LIST_CAP = 30


def _period_stats(rows: list[TradeRow]) -> dict:
    """Summary stats (N / wins / WR / PF / cumulative pts) over *rows*."""
    n = len(rows)
    wins = sum(1 for r in rows if r.net_pts > 0.0)
    cum = sum(r.net_pts for r in rows)
    gross_win = sum(r.net_pts for r in rows if r.net_pts > 0.0)
    gross_loss = abs(sum(r.net_pts for r in rows if r.net_pts < 0.0))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (None if gross_win > 0 else 0.0)
    return {
        "n": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate": round(wins / n, 4) if n else None,
        "profit_factor": (None if pf is None else round(pf, 4)),
        "cum_pts": round(cum, 1),
        # Gross win/loss exposed so the front-end can aggregate a correct
        # combined PF across years + the live feed (PF isn't additive).
        "gross_win": round(gross_win, 1),
        "gross_loss": round(gross_loss, 1),
    }


def _trigger_history(rows: list[TradeRow]) -> dict:
    """Year-bucketed backtest trigger history the dashboard merges with the
    LIVE broker feed. ``by_year[YYYY] = {<stats>, trades:[{t,d,p}, ...last 30]}``
    (JST year), newest-first trades. The live feed supplies years beyond the
    backtest's coverage; the front-end concatenates them."""
    by_year_rows: dict[int, list[TradeRow]] = {}
    for r in rows:
        by_year_rows.setdefault(r.entry_year, []).append(r)
    by_year: dict[str, dict] = {}
    for year in sorted(by_year_rows.keys()):
        yr = by_year_rows[year]
        ordered = sorted(yr, key=lambda r: r.entry_ms, reverse=True)
        trades = [{"t": r.entry_ms, "d": r.direction, "p": round(r.net_pts, 1)}
                  for r in ordered[:TRIGGER_LIST_CAP]]
        by_year[str(year)] = {**_period_stats(yr), "trades": trades}
    # Last backtest year in UTC. ``entry_year`` is JST, so a 2025-12-31 UTC
    # boundary trade can spill into a stray JST-2026 bucket; the front-end
    # uses this UTC last-year as the fixed CSV↔live boundary so a stray year
    # never lets the backtest silently own (and suppress) the live 2026 feed.
    last_year_utc = 0
    for r in rows:
        uy = pd.Timestamp(r.entry_ms, unit="ms", tz="UTC").year
        if uy > last_year_utc:
            last_year_utc = uy
    return {"by_year": by_year, "last_year": last_year_utc}




def _hourly_breakdown(rows: list[TradeRow]) -> list[dict]:
    """Per-JST-hour win rate (0-23). Returns a fixed 24-element list so the
    front-end heatmap always has every slot, even hours with no trades."""
    buckets: dict[int, list[TradeRow]] = {h: [] for h in range(24)}
    for r in rows:
        buckets[r.entry_hour_jst].append(r)
    out: list[dict] = []
    for h in range(24):
        sub = buckets[h]
        n = len(sub)
        wins = sum(1 for r in sub if r.net_pts > 0.0)
        out.append({
            "hour": h,
            "n": n,
            "wins": wins,
            "win_rate": round(wins / n, 4) if n else None,
        })
    return out


# --------------------------------------------------------------------------- #
# Statistical helpers — pure
# --------------------------------------------------------------------------- #

def _aggregate(nets: list[float], maes: list[float]) -> dict[str, float | int | str]:
    """All-period statistic bundle for a list of net P/Ls."""
    s = summarize_pnls(nets)
    n = int(s["n"])
    wins = sum(1 for p in nets if p > 0.0)
    ci_low, ci_high = wilson_interval(wins, n)
    be = breakeven_win_rate(nets)
    # Stability check across thirds — same as live tier classifier uses.
    thirds = _split_three(nets)
    third_exp = [sum(t) / len(t) if t else 0.0 for t in thirds]
    tier = classify_tier(
        n_trades=n,
        ci_low=ci_low,
        breakeven=be,
        thirds_expectancy=third_exp,
    )
    return {
        "n": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate": s["win_rate"],
        "ci_low_wilson": ci_low,
        "ci_high_wilson": ci_high,
        "breakeven_wr": be,
        "profit_factor": (None if s["profit_factor"] == float("inf")
                          else float(s["profit_factor"])),
        "expectancy": s["expectancy"],
        "max_drawdown": s["max_drawdown"],
        "avg_mae": (sum(maes) / len(maes)) if maes else 0.0,
        "tier": tier,
        "third_expectancy": third_exp,
    }


def _split_three(items: list[float]) -> tuple[list[float], list[float], list[float]]:
    """Chronological 3-way split — remainders go to later slices."""
    n = len(items)
    base = n // 3
    extra = n - base * 3
    cut1 = base
    cut2 = base + base + (1 if extra >= 1 else 0)
    return items[:cut1], items[cut1:cut2], items[cut2:]


def _two_proportion_z(w1: int, n1: int, w2: int, n2: int) -> tuple[float, float]:
    """Pooled 2-proportion z-test on win-rate equality. Returns (z, p_two_sided).

    Uses the standard pooled variance form. Falls back to (0.0, 1.0) when
    either sample is empty so callers do not need to guard.
    """
    if n1 == 0 or n2 == 0:
        return 0.0, 1.0
    p1 = w1 / n1
    p2 = w2 / n2
    p_pool = (w1 + w2) / (n1 + n2)
    se = math.sqrt(p_pool * (1.0 - p_pool) * (1.0 / n1 + 1.0 / n2))
    if se <= 0.0:
        return 0.0, 1.0
    z = (p1 - p2) / se
    # Two-sided p via the standard normal survival function.
    p_two = 2.0 * (1.0 - _normal_cdf(abs(z)))
    return float(z), float(p_two)


def _welch_t(x: list[float], y: list[float]) -> tuple[float, float]:
    """Welch's t-test on two independent samples (means). Returns (t, p_two_sided).

    Two-sided p approximated via the standard-normal CDF (valid for the
    sample sizes here: thousands of trades per period → df is large enough
    that the t distribution is indistinguishable from N(0,1)).
    """
    n1, n2 = len(x), len(y)
    if n1 < 2 or n2 < 2:
        return 0.0, 1.0
    m1, m2 = sum(x) / n1, sum(y) / n2
    v1 = sum((a - m1) ** 2 for a in x) / (n1 - 1)
    v2 = sum((a - m2) ** 2 for a in y) / (n2 - 1)
    se = math.sqrt(v1 / n1 + v2 / n2)
    if se <= 0.0:
        return 0.0, 1.0
    t = (m1 - m2) / se
    p_two = 2.0 * (1.0 - _normal_cdf(abs(t)))
    return float(t), float(p_two)


def _normal_cdf(x: float) -> float:
    """Φ(x) via the standard error function — no scipy dependency."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _moving_block_bootstrap_wr_ci(
    nets: np.ndarray,
    *,
    iters: int = BOOTSTRAP_ITER,
    block_size: int = BOOTSTRAP_BLOCK,
    seed: int = BOOTSTRAP_SEED,
    alpha: float = 0.05,
) -> tuple[float, float, float]:
    """Moving-block bootstrap 100·(1-α)% CI on win rate.

    Handles trade-to-trade autocorrelation (a Bernoulli-independent Wilson
    CI is too tight when consecutive triggers share market regime).

    Returns ``(ci_low, ci_high, std_err)``.
    """
    n = nets.size
    if n < block_size:
        # Sample too small for block resampling — return Wilson CI as a
        # honest fallback (zero block adjustment).
        wins = int((nets > 0.0).sum())
        lo, hi = wilson_interval(wins, n)
        return lo, hi, 0.0
    wins_per_trade = (nets > 0.0).astype(np.int8)
    rng = np.random.default_rng(seed)
    # Pre-build the block-start universe — every contiguous block of length
    # block_size that fits inside the array.
    block_starts = np.arange(n - block_size + 1, dtype=np.int64)
    n_blocks = math.ceil(n / block_size)
    wrs = np.empty(iters, dtype=np.float64)
    for i in range(iters):
        starts = rng.choice(block_starts, size=n_blocks, replace=True)
        # Build the resampled outcomes by indexing per chosen block, then
        # truncate to n so every bootstrap sample is exactly the same size
        # as the original — keeps the WR estimator unbiased.
        idx = (starts[:, None] + np.arange(block_size, dtype=np.int64)[None, :]).ravel()[:n]
        wrs[i] = wins_per_trade[idx].mean()
    lo = float(np.quantile(wrs, alpha / 2.0))
    hi = float(np.quantile(wrs, 1.0 - alpha / 2.0))
    se = float(wrs.std(ddof=1))
    return lo, hi, se


# --------------------------------------------------------------------------- #
# Period analysis — year-by-year and chronological 2-split
# --------------------------------------------------------------------------- #

def _year_breakdown(rows: list[TradeRow]) -> list[dict]:
    """Per calendar year: N, WR, Wilson CI, PF, EV, DD, avg MAE."""
    by_year: dict[int, list[TradeRow]] = {}
    for r in rows:
        by_year.setdefault(r.entry_year, []).append(r)
    out: list[dict] = []
    for year in sorted(by_year.keys()):
        sub = by_year[year]
        nets = [r.net_pts for r in sub]
        maes = [r.mae_pts for r in sub]
        out.append({"year": year, **_aggregate(nets, maes)})
    return out


def _period_split(rows: list[TradeRow]) -> dict:
    """2010-2017 vs 2018-2025 chronological split + drift tests.

    Both halves are evaluated with the identical deterministic rule. No
    training, no fitting — only the split is chronological so the second
    half is, by construction, unseen by any rule-design decision.
    """
    early = [r for r in rows if r.entry_year < PERIOD_SPLIT_YEAR]
    late  = [r for r in rows if r.entry_year >= PERIOD_SPLIT_YEAR]
    early_stats = _aggregate([r.net_pts for r in early],
                             [r.mae_pts for r in early])
    late_stats = _aggregate([r.net_pts for r in late],
                            [r.mae_pts for r in late])

    # 2-proportion z-test on WR.
    z_wr, p_wr_raw = _two_proportion_z(
        int(early_stats["wins"]), int(early_stats["n"]),
        int(late_stats["wins"]),  int(late_stats["n"]),
    )

    # Welch's t on expectancy (net_pts per trade).
    t_exp, p_exp_raw = _welch_t(
        [r.net_pts for r in early], [r.net_pts for r in late],
    )

    drift_wr_pp = (late_stats["win_rate"] - early_stats["win_rate"]) * 100.0
    drift_exp = late_stats["expectancy"] - early_stats["expectancy"]

    # Verdict: stable if both p > Bonferroni-α; drift if either is significant
    # but drift_wr_pp magnitude < 5pp; regime change if magnitude >= 5pp AND
    # significant.
    if p_wr_raw < BONFERRONI_ALPHA or p_exp_raw < BONFERRONI_ALPHA:
        if abs(drift_wr_pp) >= 5.0:
            verdict = "REGIME-CHANGE"
        else:
            verdict = "DRIFT"
    else:
        verdict = "STABLE"

    return {
        "split_year": PERIOD_SPLIT_YEAR,
        "early": {"period": f"2010-{PERIOD_SPLIT_YEAR-1}", **early_stats},
        "late":  {"period": f"{PERIOD_SPLIT_YEAR}-2025",   **late_stats},
        "drift_wr_pp": drift_wr_pp,
        "drift_expectancy_pts": drift_exp,
        "z_wr": z_wr,
        "p_wr_raw": p_wr_raw,
        "p_wr_bonferroni_significant": p_wr_raw < BONFERRONI_ALPHA,
        "t_expectancy": t_exp,
        "p_expectancy_raw": p_exp_raw,
        "p_expectancy_bonferroni_significant": p_exp_raw < BONFERRONI_ALPHA,
        "bonferroni_alpha": BONFERRONI_ALPHA,
        "verdict": verdict,
    }


# --------------------------------------------------------------------------- #
# Per-TF pipeline
# --------------------------------------------------------------------------- #

def _run_one_tf(
    base_tf: str,
    result,
    frames: dict[str, pd.DataFrame],
    point: float,
) -> dict:
    window = result.by_base.get(base_tf)
    if window is None:
        return {"base_tf": base_tf, "error": "no_window"}
    base_df = frames[base_tf]
    rows = _build_trade_rows(window, base_df, point)
    if not rows:
        return {"base_tf": base_tf, "error": "no_trades"}

    nets = [r.net_pts for r in rows]
    maes = [r.mae_pts for r in rows]
    aggregate = _aggregate(nets, maes)

    # Block bootstrap CI (honest under autocorrelation).
    nets_arr = np.array(nets, dtype=np.float64)
    boot_lo, boot_hi, boot_se = _moving_block_bootstrap_wr_ci(nets_arr)

    year_rows = _year_breakdown(rows)
    period = _period_split(rows)
    hourly = _hourly_breakdown(rows)
    trig_hist = _trigger_history(rows)

    log.info(
        "%s  N=%d  WR=%.2f%%  Wilson CI=[%.1f,%.1f]  Bootstrap CI=[%.1f,%.1f]  PF=%s  tier=%s",
        base_tf, aggregate["n"], aggregate["win_rate"] * 100,
        aggregate["ci_low_wilson"] * 100, aggregate["ci_high_wilson"] * 100,
        boot_lo * 100, boot_hi * 100,
        ("inf" if aggregate["profit_factor"] is None
         else f"{aggregate['profit_factor']:.3f}"),
        aggregate["tier"],
    )

    return {
        "base_tf": base_tf,
        "aggregate": aggregate,
        "bootstrap_ci": {
            "method": f"moving-block, B={BOOTSTRAP_BLOCK}, iters={BOOTSTRAP_ITER}",
            "ci_low": boot_lo,
            "ci_high": boot_hi,
            "std_err": boot_se,
            "wilson_vs_bootstrap_widen_pp": (boot_hi - boot_lo) * 100
                - (aggregate["ci_high_wilson"] - aggregate["ci_low_wilson"]) * 100,
        },
        "year_breakdown": year_rows,
        "period_split": period,
        "hourly_winrate": hourly,
        "trigger_history": trig_hist,
    }


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def _write_markdown_multi(payload: dict) -> None:
    """Human-readable summary for the multi-symbol payload."""
    lines: list[str] = []
    lines.append("# 16-year deep-history OOS validation (multi-symbol)\n\n")
    lines.append(f"Generated: {payload['generated_at_iso']}\n\n")
    lines.append(f"Source: {payload['source']}\n\n")
    lines.append("Year filter: **NONE**. Warmup skip: **NONE**. "
                 "Indicator NaN drop is the only natural attenuation.\n\n")
    lines.append(f"Bonferroni α (k={BONFERRONI_K} base TFs) = "
                 f"{BONFERRONI_ALPHA:.4f}\n\n")

    for sym, sym_payload in payload["by_symbol"].items():
        if isinstance(sym_payload, dict) and "error" in sym_payload:
            lines.append(f"\n# {sym} — SKIPPED ({sym_payload['error']})\n")
            continue
        lines.append(f"\n# {sym}\n")
        _write_symbol_md(sym_payload, lines)

    MD_OUT.parent.mkdir(parents=True, exist_ok=True)
    MD_OUT.write_text("".join(lines), encoding="utf-8")


def _write_symbol_md(by_tf: dict, lines: list[str]) -> None:
    """Append the per-TF section for one symbol to *lines*."""
    for tf in BASE_TFS:
        cell = by_tf.get(tf)
        if not cell or "error" in cell:
            lines.append(f"\n## {tf} — NO DATA\n")
            continue
        agg = cell["aggregate"]
        boot = cell["bootstrap_ci"]
        ps = cell["period_split"]
        lines.append(f"\n## Base TF = {tf}\n\n")
        lines.append("### Aggregate (all 16 years)\n\n")
        lines.append(f"- N = {agg['n']:,d}\n")
        lines.append(f"- Win rate = **{agg['win_rate']*100:.2f}%**\n")
        lines.append(f"- Wilson 95% CI (independent) = "
                     f"[{agg['ci_low_wilson']*100:.2f}%, "
                     f"{agg['ci_high_wilson']*100:.2f}%]\n")
        lines.append(f"- **Block-bootstrap 95% CI (autocorrelation-honest)** = "
                     f"[{boot['ci_low']*100:.2f}%, {boot['ci_high']*100:.2f}%]  "
                     f"(Wilson is {abs(boot['wilson_vs_bootstrap_widen_pp']):.2f} pp "
                     f"{'tighter' if boot['wilson_vs_bootstrap_widen_pp'] > 0 else 'wider'})\n")

        pf = agg["profit_factor"]
        lines.append(f"- Profit factor = {('∞' if pf is None else f'{pf:.3f}')}\n")
        lines.append(f"- Expectancy = {agg['expectancy']:+.1f} pts/trade\n")
        lines.append(f"- Max drawdown = {agg['max_drawdown']:.1f} pts\n")
        lines.append(f"- Breakeven WR = {agg['breakeven_wr']*100:.2f}%\n")
        lines.append(f"- Tier = **{agg['tier']}**\n\n")

        lines.append("### Year breakdown\n\n")
        lines.append("| year | N | WR | CI95 (Wilson) | PF | EV | DD |\n")
        lines.append("|---:|---:|---:|---|---:|---:|---:|\n")
        for yr in cell["year_breakdown"]:
            pf_y = yr["profit_factor"]
            lines.append(
                f"| {yr['year']} | {yr['n']:,d} | "
                f"{yr['win_rate']*100:.1f}% | "
                f"[{yr['ci_low_wilson']*100:.1f}, {yr['ci_high_wilson']*100:.1f}] | "
                f"{('∞' if pf_y is None else f'{pf_y:.2f}')} | "
                f"{yr['expectancy']:+.0f} | "
                f"{yr['max_drawdown']:.0f} |\n"
            )

        lines.append("\n### Chronological period split (2010-2017 vs 2018-2025)\n\n")
        e, l = ps["early"], ps["late"]
        lines.append(f"- **Early** ({e['period']}): N={e['n']:,d}  "
                     f"WR={e['win_rate']*100:.2f}%  "
                     f"PF={('∞' if e['profit_factor'] is None else f'{e['profit_factor']:.2f}')}  "
                     f"EV={e['expectancy']:+.1f}\n")
        lines.append(f"- **Late** ({l['period']}): N={l['n']:,d}  "
                     f"WR={l['win_rate']*100:.2f}%  "
                     f"PF={('∞' if l['profit_factor'] is None else f'{l['profit_factor']:.2f}')}  "
                     f"EV={l['expectancy']:+.1f}\n")
        lines.append(f"- WR drift = {ps['drift_wr_pp']:+.2f} pp  "
                     f"(z = {ps['z_wr']:+.3f}, p_raw = {ps['p_wr_raw']:.4e}, "
                     f"Bonferroni-significant: "
                     f"{'YES' if ps['p_wr_bonferroni_significant'] else 'no'})\n")
        lines.append(f"- EV drift = {ps['drift_expectancy_pts']:+.1f} pts  "
                     f"(t = {ps['t_expectancy']:+.3f}, p_raw = {ps['p_expectancy_raw']:.4e}, "
                     f"Bonferroni-significant: "
                     f"{'YES' if ps['p_expectancy_bonferroni_significant'] else 'no'})\n")
        lines.append(f"- **Verdict: {ps['verdict']}**\n")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def _run_one_symbol(symbol: str, point: float) -> dict:
    """Load CSVs for *symbol*, run DWS-SMT, build the full per-TF report.

    Returns the same shape as ``by_symbol[symbol]`` in the JSON payload:
    ``{base_tf: {...}}``. Missing CSVs produce ``{"error": "csv_missing"}``.
    """
    try:
        frames = {tf: _load_tf(symbol, tf, point)
                  for tf in ("W1", "D1", "H4", "H1", "M15")}
    except FileNotFoundError as exc:
        log.warning("%s: CSV missing (%s) — skipping", symbol, exc)
        return {"error": "csv_missing", "detail": str(exc)}

    for tf in ("W1", "D1", "H4", "H1", "M15"):
        df = frames[tf]
        log.info("  %s %s: bars=%d  %s → %s UTC",
                 symbol, tf, len(df),
                 df.index.min().strftime("%Y-%m-%d %H:%M"),
                 df.index.max().strftime("%Y-%m-%d %H:%M"))

    emit = max(len(df) for df in frames.values()) + 100
    t1 = time.perf_counter()
    result = dws_smt.compute_symbol(
        frames=frames,
        stacks=config.DWS_SMT_STACKS,
        period=config.DWS_SMT_PERIOD,
        smooth=config.DWS_SMT_SMOOTH,
        out_bars=emit,
    )
    if result is None:
        return {"error": "compute_symbol_returned_none"}
    log.info("  %s DWS compute %.1fs", symbol, time.perf_counter() - t1)

    by_tf: dict[str, dict] = {}
    for base_tf in BASE_TFS:
        by_tf[base_tf] = _run_one_tf(base_tf, result, frames, point)
    return by_tf


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    t0 = time.perf_counter()
    symbols = [s.base for s in config.SYMBOLS]
    log.info("16y full OOS for %d symbols × 3 base TFs — NO filter, NO warmup skip",
             len(symbols))

    by_symbol: dict[str, dict] = {}
    for sym in symbols:
        point = POINT_BY_SYMBOL.get(sym)
        if point is None:
            log.warning("%s: no POINT entry — skipping", sym)
            continue
        log.info("=== %s (point=%g) ===", sym, point)
        result = _run_one_symbol(sym, point)
        by_symbol[sym] = result

    payload = {
        "schema_version": 2,
        "generated_at": time.time(),
        "generated_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "Dukascopy CSV W1/D1/H4/H1/M15, full history "
                  "(W1: 2009-12-28 onwards, others: 2010-01-01 onwards), "
                  "through 2025-12-31. NO year filter. NO warmup skip.",
        "compute_method": "production dws_smt.compute_symbol + "
                          "signal_validator.evaluate_trades",
        "spread_source": "Dukascopy bid-ask close (per-bar)",
        "swap_costs_applied": False,
        "bonferroni_alpha": BONFERRONI_ALPHA,
        "bonferroni_k": BONFERRONI_K,
        "bootstrap": {
            "method": "moving block",
            "iters": BOOTSTRAP_ITER,
            "block_size": BOOTSTRAP_BLOCK,
            "seed": BOOTSTRAP_SEED,
        },
        "period_split_year": PERIOD_SPLIT_YEAR,
        "by_symbol": by_symbol,
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    JSON_OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    _write_markdown_multi(payload)
    log.info("wrote %s", JSON_OUT)
    log.info("wrote %s", MD_OUT)

    # Emit a UI-facing oos_baseline.json with the flat back-compat shape PLUS
    # the new rich fields the dashboard will progressively pick up. Old fields
    # (n_trades, win_rate, ci_low, ci_high, profit_factor, expectancy,
    # max_drawdown, tier) stay at the same path so the existing front-end
    # 16Y reference line keeps rendering with zero JS changes; new fields
    # (bootstrap_ci, period_split, year_breakdown) live alongside them.
    compat_path = OUT_DIR / "oos_baseline.json"
    compat_payload = {
        "schema_version": 2,
        "generated_at": payload["generated_at"],
        "generated_at_iso": payload["generated_at_iso"],
        "source": payload["source"],
        "compute_method": payload["compute_method"],
        "year_filter": "NONE",
        "warmup_skip": "NONE",
        "bonferroni_alpha": payload["bonferroni_alpha"],
        "bonferroni_k": payload["bonferroni_k"],
        "bootstrap": payload["bootstrap"],
        "period_split_year": payload["period_split_year"],
        "by_symbol": _flatten_for_ui(payload["by_symbol"]),
    }
    compat_path.write_text(json.dumps(compat_payload, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    log.info("wrote %s (UI-facing flat schema)", compat_path)

    log.info("total elapsed: %.1fs", time.perf_counter() - t0)
    return 0


def _flatten_for_ui(by_symbol_rich: dict) -> dict:
    """Convert the rich nested schema to the UI's flat per-cell shape.

    UI legacy keys at ``by_symbol[sym][tf]``:
        n_trades, win_rate, ci_low, ci_high, profit_factor, expectancy,
        max_drawdown, tier

    Plus the new rich block:
        bootstrap_ci, period_split, year_breakdown, breakeven_wr, avg_mae

    Missing/error cells are emitted as ``{"error": ...}`` so the UI can
    quietly hide that row instead of crashing on a missing key.
    """
    out: dict[str, dict] = {}
    for sym, by_tf in by_symbol_rich.items():
        if not isinstance(by_tf, dict) or "error" in by_tf:
            out[sym] = {"error": by_tf.get("error") if isinstance(by_tf, dict) else "unknown"}
            continue
        sym_out: dict[str, dict] = {}
        for tf, cell in by_tf.items():
            if not isinstance(cell, dict) or "error" in cell:
                sym_out[tf] = {"error": cell.get("error") if isinstance(cell, dict) else "unknown"}
                continue
            agg = cell["aggregate"]
            pf = agg["profit_factor"]
            sym_out[tf] = {
                # Legacy UI fields — same names + shapes as the old baseline.
                "n_trades": int(agg["n"]),
                "win_rate": float(agg["win_rate"]),
                "ci_low":   float(agg["ci_low_wilson"]),
                "ci_high":  float(agg["ci_high_wilson"]),
                "profit_factor": pf,
                "expectancy": float(agg["expectancy"]),
                "max_drawdown": float(agg["max_drawdown"]),
                "tier": agg["tier"],
                # New rich fields — UI will pick these up next.
                "wins":  int(agg["wins"]),
                "losses": int(agg["losses"]),
                "breakeven_wr": float(agg["breakeven_wr"]),
                "avg_mae": float(agg["avg_mae"]),
                "bootstrap_ci": {
                    "ci_low":  float(cell["bootstrap_ci"]["ci_low"]),
                    "ci_high": float(cell["bootstrap_ci"]["ci_high"]),
                    "std_err": float(cell["bootstrap_ci"]["std_err"]),
                    "method":  cell["bootstrap_ci"]["method"],
                },
                "period_split": cell["period_split"],
                "year_breakdown": cell["year_breakdown"],
                "hourly_winrate": cell.get("hourly_winrate", []),
                "trigger_history": cell.get("trigger_history", {}),
            }
        out[sym] = sym_out
    return out


if __name__ == "__main__":
    sys.exit(main())
