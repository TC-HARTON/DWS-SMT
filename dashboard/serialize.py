"""Convert backend snapshots into JSON-serialisable dicts for the browser.

The WebSocket broadcaster ships the output of :func:`snapshot_to_json`
verbatim, and the clientside callbacks in ``dashboard/callbacks.py``
expect exactly this shape. Keep the keys stable and add new ones rather
than rename — the front end has no schema version handling beyond the
``version`` monotonic counter at the top level.

Numeric values that may be ``None`` (warmup) are kept as ``None`` rather
than ``NaN`` so the JS side gets a plain ``null`` and can render "n/a".
"""

from __future__ import annotations

import logging
from typing import Any, Final

import config

_log = logging.getLogger(__name__)

from analyzer.account_monitor import (
    PerformanceSnapshot, RangeStats, SymbolStats, ClosedTrade,
    AdvancedStats, EdgeStats)
from analyzer.calendar_feed import CalendarEvent, CalendarSnapshot
from analyzer.cot_feed import CotSnapshot
from analyzer.dxy_feed import DxySnapshot
from analyzer.ema_stack import EmaStackSnapshot
from analyzer.indicator_engine import (
    AnalysisSnapshot,
    ChartBars,
    SymbolIndicators,
    TimeframeIndicators,
)
from analyzer.macro_feed import (
    MacroEmployment,
    MacroPairBias,
    MacroRate,
    MacroSnapshot,
    RealYieldSnapshot,
)
from analyzer.mt5_connector import AccountSnapshot, Tick
from analyzer.position_sizing import recommended_lot
from analyzer.state import (
    ConnectionStatus,
    LatestState,
    PriceSnapshot,
)


def _opt_float(v: float | None) -> float | None:
    """Pass through floats but coerce NaN and Inf to None for valid JSON.

    MT5 emits ``Inf`` for ``margin_level`` whenever margin is zero (no open
    positions), and ``json.dumps`` raises ``ValueError`` on Inf — without
    this coercion every WS client disconnects on flat accounts.
    """
    if v is None:
        return None
    fv = float(v)
    if fv != fv or fv == float("inf") or fv == float("-inf"):
        return None
    return fv


def serialize_tick(t: Tick) -> dict[str, Any]:
    return {
        "symbol": t.symbol,
        "bid": _opt_float(t.bid),
        "ask": _opt_float(t.ask),
        "last": _opt_float(t.last),
        "time_msc": t.time_msc,
    }


def serialize_price(p: PriceSnapshot | None) -> dict[str, Any] | None:
    if p is None:
        return None
    return {
        "generated_at": p.generated_at,
        "ticks": {base: serialize_tick(t) for base, t in p.ticks.items()},
    }


def serialize_timeframe(tf: TimeframeIndicators) -> dict[str, Any]:
    return {
        "label": tf.label,
        "last_close": _opt_float(tf.last_close),
        "ema": _opt_float(tf.ema),
        "ema_period": tf.ema_period,
        "above_ema": tf.above_ema,
        "rsi": _opt_float(tf.rsi),
        "atr": _opt_float(tf.atr),
        "adx": _opt_float(tf.adx),
        "di_plus": _opt_float(tf.di_plus),
        "di_minus": _opt_float(tf.di_minus),
        "bar_time": tf.bar_time.isoformat() if tf.bar_time is not None else None,
    }


def serialize_chart(c: ChartBars | None) -> dict[str, Any] | None:
    if c is None:
        return None
    return {
        "ohlc_h4": [[float(o), float(h), float(l), float(cl)]
                    for (o, h, l, cl) in c.ohlc_h4],
        "closes_m15": [float(v) for v in c.closes_m15],
        "ema_h4": _opt_float(c.ema_h4),
    }


def serialize_symbol(sym: SymbolIndicators) -> dict[str, Any]:
    return {
        "base": sym.base,
        "broker_name": sym.broker_name,
        "by_tf": {label: serialize_timeframe(tf) for label, tf in sym.by_tf.items()},
        "chart": serialize_chart(sym.chart),
    }


def serialize_analysis(a: AnalysisSnapshot | None) -> dict[str, Any] | None:
    if a is None:
        return None
    return {
        "generated_at": a.generated_at.isoformat(),
        "compute_ms": float(a.compute_ms),
        "by_symbol": {base: serialize_symbol(s) for base, s in a.by_symbol.items()},
    }


def serialize_account(acc: AccountSnapshot | None) -> dict[str, Any] | None:
    if acc is None:
        return None
    return {
        "login": acc.login,
        "server": acc.server,
        "company": acc.company,
        "currency": acc.currency,
        "balance": _opt_float(acc.balance),
        "equity": _opt_float(acc.equity),
        "profit": _opt_float(acc.profit),
        "margin": _opt_float(acc.margin),
        "margin_free": _opt_float(acc.margin_free),
        "margin_level": _opt_float(acc.margin_level),
        "leverage": int(acc.leverage),
        # Both account + terminal permit live trading — the order panel enables
        # its BUY/SELL/close buttons only when this is true.
        "trade_allowed": bool(getattr(acc, "trade_allowed", False)),
        # Recommended trade size from the validated fixed-fractional lot ladder
        # (single source of truth in analyzer.position_sizing). Sized off the
        # settled BALANCE (not equity) so the recommendation does not wobble
        # with open floating P/L. lot_rule carries the params so the UI can
        # render the rule subtitle without hard-coding.
        "recommended_lot": recommended_lot(acc.balance),
        "lot_rule": {
            "base": config.LOT_BASE,
            "step": config.LOT_EQUITY_STEP,
            "max": config.LOT_MAX,
        },
        "positions": [
            {
                "ticket": p.ticket,
                "symbol": p.symbol,
                "type": p.type,
                "volume": _opt_float(p.volume),
                "price_open": _opt_float(p.price_open),
                "price_current": _opt_float(p.price_current),
                "sl": _opt_float(p.sl),
                "tp": _opt_float(p.tp),
                "profit": _opt_float(p.profit),
                "swap": _opt_float(p.swap),
                "time": int(p.time),
            }
            for p in acc.positions
        ],
    }


def serialize_status(s: ConnectionStatus) -> dict[str, Any]:
    return {
        "connected": bool(s.connected),
        "last_error": s.last_error,
        "last_connect_ts": s.last_connect_ts,
    }


# --------------------------------------------------------------------------- #
# Phase 3: performance
# --------------------------------------------------------------------------- #

def serialize_symbol_stats(s: SymbolStats) -> dict[str, Any]:
    return {
        "symbol": s.symbol,
        "trade_count": int(s.trade_count),
        "win_count": int(s.win_count),
        "net_profit": _opt_float(s.net_profit),
        "win_rate": _opt_float(s.win_rate),
    }


def serialize_range_stats(r: RangeStats) -> dict[str, Any]:
    return {
        "range_label": r.range_label,
        "trade_count": int(r.trade_count),
        "win_count": int(r.win_count),
        "loss_count": int(r.loss_count),
        "win_rate": _opt_float(r.win_rate),
        "profit_factor": _opt_float(r.profit_factor),
        "max_drawdown_abs": _opt_float(r.max_drawdown_abs),
        "max_drawdown_pct": _opt_float(r.max_drawdown_pct),
        "avg_win": _opt_float(r.avg_win),
        "avg_loss": _opt_float(r.avg_loss),
        "risk_reward": _opt_float(r.risk_reward),
        "gross_profit": _opt_float(r.gross_profit),
        "gross_loss": _opt_float(r.gross_loss),
        "net_profit": _opt_float(r.net_profit),
        "by_symbol": {sym: serialize_symbol_stats(ss)
                      for sym, ss in r.by_symbol.items()},
        "by_hour_jst": {str(h): _opt_float(v) for h, v in r.by_hour_jst.items()},
    }


def serialize_closed_trade(t: ClosedTrade) -> dict[str, Any]:
    return {
        "position_id": int(t.position_id),
        "symbol": t.symbol,
        "type": t.type,
        "volume": _opt_float(t.volume),
        "entry_time": float(t.entry_time),
        "exit_time": float(t.exit_time),
        "entry_price": _opt_float(t.entry_price),
        "exit_price": _opt_float(t.exit_price),
        "profit": _opt_float(t.profit + t.swap + t.commission),
        "pips": _opt_float(t.pips),
        "mae_pips": _opt_float(t.mae_pips),
        "mfe_pips": _opt_float(t.mfe_pips),
        "r_multiple": _opt_float(t.r_multiple),
        "sl": _opt_float(t.sl),
        "tp": _opt_float(t.tp),
        "ctx": t.ctx,
        "env": t.env,
    }


def serialize_advanced_stats(a: AdvancedStats | None) -> dict[str, Any] | None:
    if a is None:
        return None
    return {
        "sharpe": _opt_float(a.sharpe),
        "sortino": _opt_float(a.sortino),
        "calmar": _opt_float(a.calmar),
        "recovery_factor": _opt_float(a.recovery_factor),
        "ulcer_index": _opt_float(a.ulcer_index),
        "var_95": _opt_float(a.var_95),
        "cvar_95": _opt_float(a.cvar_95),
        "max_win_streak": int(a.max_win_streak),
        "max_loss_streak": int(a.max_loss_streak),
        "current_streak": int(a.current_streak),
        "max_drawdown_abs": _opt_float(a.max_drawdown_abs),
        "underwater_pct": _opt_float(a.underwater_pct),
        "r_distribution": {k: int(v) for k, v in a.r_distribution.items()},
        "equity_curve": [_opt_float(x) for x in a.equity_curve],
        "underwater_curve": [_opt_float(x) for x in a.underwater_curve],
    }


def serialize_edge_stats(e: EdgeStats | None) -> dict[str, Any] | None:
    if e is None:
        return None
    return {
        "by_alignment": e.by_alignment,
        "by_adx": e.by_adx,
        "by_rsi": e.by_rsi,
        "by_weekday_jst": e.by_weekday_jst,
        "by_hold_min": e.by_hold_min,
        "by_dxy": e.by_dxy,
        "by_cot_extreme": e.by_cot_extreme,
        "by_real_yield": e.by_real_yield,
        "by_flip": e.by_flip,
    }


def serialize_performance(s: PerformanceSnapshot | None) -> dict[str, Any] | None:
    if s is None:
        return None
    return {
        "generated_at": float(s.generated_at),
        "compute_ms": float(s.compute_ms),
        "fetched_from_ts": float(s.fetched_from_ts),
        "fetched_to_ts": float(s.fetched_to_ts),
        "open_trade_count": int(s.open_trade_count),
        "today_realised_pnl": _opt_float(s.today_realised_pnl),
        "today_floating_pnl": _opt_float(s.today_floating_pnl),
        "today_total_pnl": _opt_float(s.today_total_pnl),
        "ranges": [r.label for r in config.HISTORY_RANGES],
        "default_range": config.HISTORY_DEFAULT_RANGE,
        "by_range": {label: serialize_range_stats(rs)
                     for label, rs in s.by_range.items()},
        "trades": [serialize_closed_trade(t) for t in s.trades],
        "advanced": serialize_advanced_stats(s.advanced),
        "edge": serialize_edge_stats(s.edge),
    }


# Central-bank rate-decision label keyed by the event's currency. The English
# title still drives the upstream keyword filter; this only localises display.
_JP_RATE_BY_CCY: Final[dict[str, str]] = {
    "USD": "FOMC 政策金利発表",
    "EUR": "ECB 政策金利発表",
    "GBP": "BoE 政策金利発表",
    "JPY": "日銀 金融政策決定会合",
    "AUD": "RBA 政策金利発表",
}
_JP_RATE_KEYWORDS: Final[tuple[str, ...]] = (
    "fomc", "ecb", "boe", "boj", "rba", "governing council", "mpc",
    "bank rate", "cash rate", "rate decision", "rate statement",
    "monetary policy", "interest rate", "federal funds", "refinancing",
    "policy rate",
)


def _jp_calendar_title(title: str, ccy: str) -> str:
    """Localise a high-impact event title to Japanese for display.

    Employment releases map by type; rate decisions map to the right central
    bank via the event currency. Anything unrecognised falls back to the
    original title so nothing is ever dropped."""
    t = title.lower()
    # ADP is "ADP Non-Farm Employment Change" — a PRIVATE payroll estimate, NOT
    # the official BLS NFP. Match it FIRST so it doesn't collapse into "NFP".
    if "adp" in t:
        return "ADP雇用統計"
    if any(k in t for k in ("non-farm", "nonfarm", "payroll")):
        return "米雇用統計 (NFP)"
    if any(k in t for k in ("unemployment", "jobless", "claimant")):
        return "失業率"
    if "press conference" in t:
        return "中銀総裁会見"
    if any(k in t for k in _JP_RATE_KEYWORDS):
        return _JP_RATE_BY_CCY.get(ccy, "政策金利発表")
    return title


# Category chip keys — derived from the ENGLISH title so the badge survives
# title localisation. Kept in sync with the front-end fallback.
_CAT_EMP_KEYWORDS: Final[tuple[str, ...]] = (
    "payroll", "nonfarm", "non-farm", "employment", "unemploy", "jobless",
    "hourly earnings", "earnings index", "claimant count", "jolts", "adp",
)
_CAT_RATE_KEYWORDS: Final[tuple[str, ...]] = (
    "fomc", "federal funds rate", "bank rate", "cash rate", "policy rate",
    "refinanc", "rate statement", "rate decision", "monetary policy",
    "interest rate", "press conference",
)


def _calendar_category(title: str) -> str:
    """Classify an event as employment / rate / other from its English title."""
    t = title.lower()
    if any(k in t for k in _CAT_EMP_KEYWORDS):
        return "emp"
    if any(k in t for k in _CAT_RATE_KEYWORDS):
        return "rate"
    return "oth"


def serialize_calendar_event(e: CalendarEvent) -> dict[str, Any]:
    # Classify from the original English title, THEN localise the display text.
    return {
        "release_ts": float(e.release_ts),
        "currency": e.currency,
        "title": _jp_calendar_title(e.title, e.currency),
        "category": _calendar_category(e.title),
        "impact": e.impact,
        "forecast": e.forecast,
        "previous": e.previous,
        "actual": e.actual,
        "source": e.source,
        "source_url": getattr(e, "source_url", ""),
    }


def serialize_calendar(s: CalendarSnapshot | None) -> dict[str, Any] | None:
    if s is None:
        return None
    return {
        "generated_at": float(s.generated_at),
        # fetched_at == 0 means "never fetched"; emit null so the front-end
        # does not render epoch 1970.
        "fetched_at": float(s.fetched_at) if s.fetched_at > 0 else None,
        "source": s.source,
        "last_error": s.last_error,
        "consecutive_failures": int(s.consecutive_failures),
        "warning_window_sec": int(config.CALENDAR_WARNING_WINDOW_SEC),
        "display_count": int(config.CALENDAR_DISPLAY_COUNT),
        "events": [serialize_calendar_event(e) for e in s.events],
    }


# --------------------------------------------------------------------------- #
# Macro / rate-differential layer (precision-optimization spec, Section B)
# --------------------------------------------------------------------------- #

def _serialize_macro_rate(r: MacroRate) -> dict[str, Any]:
    return {
        "currency": r.currency,
        "rate": _opt_float(r.rate),
        "as_of": r.as_of,
        "prev_rate": _opt_float(r.prev_rate),
        "source": r.source,
        "stale": bool(r.stale),
    }


def _serialize_macro_pair(b: MacroPairBias) -> dict[str, Any]:
    return {
        "pair": b.pair,
        "base_ccy": b.base_ccy,
        "quote_ccy": b.quote_ccy,
        "differential": _opt_float(b.differential),
        "macro_dir": int(b.macro_dir),
        "label": b.label,
    }


def _serialize_macro_employment(e: MacroEmployment | None) -> dict[str, Any] | None:
    if e is None:
        return None
    return {
        "nonfarm_change": _opt_float(e.nonfarm_change),
        "unemployment_rate": _opt_float(e.unemployment_rate),
        "as_of": e.as_of,
        "prev_nonfarm_change": _opt_float(e.prev_nonfarm_change),
        "source": e.source,
        "stale": bool(getattr(e, "stale", False)),
    }


def serialize_macro(s: MacroSnapshot | None) -> dict[str, Any] | None:
    """Serialise the macro snapshot for the WebSocket payload."""
    if s is None:
        return None
    return {
        "generated_at": float(s.generated_at),
        "fetched_at": float(s.fetched_at) if s.fetched_at > 0 else None,
        "rates": {ccy: _serialize_macro_rate(r) for ccy, r in s.rates.items()},
        "employment": _serialize_macro_employment(s.employment),
        "by_pair": {p: _serialize_macro_pair(b) for p, b in s.by_pair.items()},
        "last_error": s.last_error,
        "consecutive_failures": int(s.consecutive_failures),
    }


def serialize_real_yield(s: RealYieldSnapshot | None) -> dict[str, Any] | None:
    """Serialise the US 10Y real-yield snapshot for the WebSocket payload."""
    if s is None:
        return None
    return {
        "value": _opt_float(s.value),
        "prev_value": _opt_float(s.prev_value),
        "change_1d": _opt_float(s.change_1d),
        "trend_5d": _opt_float(s.trend_5d),
        "gold_dir": int(s.gold_dir),
        "as_of": s.as_of,
        "stale": bool(s.stale),
        "generated_at": float(s.generated_at),
        # Recent daily closes for the sidebar sparkline (DXY-style chart).
        "series": [float(v) for v in getattr(s, "series", ())],
        # Real-time overlay: live nominal 10Y (^TNX) + flag that ``value`` carries
        # the intraday adjustment (anchor + Δnominal).
        "nominal_10y": _opt_float(getattr(s, "nominal_10y", None)),
        "nominal_prev": _opt_float(getattr(s, "nominal_prev", None)),
        "is_live": bool(getattr(s, "is_live", False)),
    }


def serialize_dxy(s: DxySnapshot | None) -> dict[str, Any] | None:
    """Serialise the DXY (dollar-index) context snapshot for the WS payload.

    NaN/Inf floats coerce to ``null`` via ``_opt_float``; ``closes`` ships as a
    plain list of floats; ``symbol``/``above_ema``/``stale`` pass through.
    """
    if s is None:
        return None
    return {
        "symbol": s.symbol,
        "price": _opt_float(s.price),
        "prev_close": _opt_float(s.prev_close),
        "change": _opt_float(s.change),
        "change_pct": _opt_float(s.change_pct),
        "ema": _opt_float(s.ema),
        "above_ema": s.above_ema,
        "closes": [float(v) for v in s.closes],
        "as_of": float(s.as_of),
        "stale": bool(s.stale),
    }


def serialize_ema_stack(s: EmaStackSnapshot | None) -> dict[str, Any] | None:
    """Serialise the EMA-stack oscillator for the WS payload.

    ``t`` = epoch-ms per bar; ``dev_price`` / ``dev_fast`` / ``dev_mid`` =
    %-deviation of price / EMA20 / EMA80 from the EMA320 centerline (one value
    per bar). ``ema_*`` are the latest absolute EMA readouts.
    """
    if s is None:
        return None
    return {
        "symbol": s.symbol,
        "periods": list(s.periods),
        "price": _opt_float(s.price),
        "ema_fast": _opt_float(s.ema_fast),
        "ema_mid": _opt_float(s.ema_mid),
        "ema_center": _opt_float(s.ema_center),
        "bands": s.bands,
        "mode": s.mode,
        "t": list(s.times_ms),
        "dev_price": [float(v) for v in s.dev_price],
        "dev_fast": [float(v) for v in s.dev_fast],
        "dev_mid": [float(v) for v in s.dev_mid],
        "as_of": float(s.as_of),
        "stale": bool(s.stale),
    }


def serialize_cot(s: CotSnapshot | None) -> dict[str, Any] | None:
    """Serialise the CFTC COT (gold-positioning) snapshot for the WS payload.

    Integer counts pass through as-is (or ``null`` during warm-up); the
    percentage / percentile floats coerce NaN/Inf to ``null`` via ``_opt_float``;
    ``net_history`` / ``history_dates`` ship as parallel arrays for a sparkline.
    """
    if s is None:
        return None
    return {
        "market": s.market,
        "report_date": s.report_date,
        "noncomm_long": s.noncomm_long,
        "noncomm_short": s.noncomm_short,
        "net": s.net,
        "net_prev": s.net_prev,
        "net_change": s.net_change,
        "comm_long": s.comm_long,
        "comm_short": s.comm_short,
        "comm_net": s.comm_net,
        "open_interest": s.open_interest,
        "net_pct_oi": _opt_float(s.net_pct_oi),
        "long_share": _opt_float(s.long_share),
        "pctile_1y": _opt_float(s.pctile_1y),
        "direction": int(s.direction),
        "extreme": int(s.extreme),
        "net_history": [int(v) for v in s.net_history],
        "history_dates": list(s.history_dates),
        "fetched_at": float(s.fetched_at) if s.fetched_at > 0 else None,
        "generated_at": float(s.generated_at),
        "stale": bool(s.stale),
        "last_error": s.last_error,
    }


# --------------------------------------------------------------------------- #


def snapshot_to_json(state: LatestState) -> dict[str, Any]:
    """Render the entire :class:`LatestState` into a flat JSON-ready dict."""
    snap = state.snapshot()
    out: dict[str, Any] = {
        "version": int(snap["version"]),  # type: ignore[arg-type]
        "ts": float(snap["ts"]),  # type: ignore[arg-type]
        "status": serialize_status(snap["status"]),  # type: ignore[arg-type]
        "price": serialize_price(snap["price"]),  # type: ignore[arg-type]
        "analysis": serialize_analysis(snap["analysis"]),  # type: ignore[arg-type]
        "account": serialize_account(snap["account"]),  # type: ignore[arg-type]
        "performance": serialize_performance(snap["performance"]),  # type: ignore[arg-type]
        "calendar": serialize_calendar(snap["calendar"]),  # type: ignore[arg-type]
        "macro": serialize_macro(snap["macro"]),  # type: ignore[arg-type]
        "real_yield": serialize_real_yield(snap["real_yield"]),  # type: ignore[arg-type]
        "dxy": serialize_dxy(snap.get("dxy")),  # type: ignore[arg-type]
        "ema_stack": serialize_ema_stack(snap.get("ema_stack")),  # type: ignore[arg-type]
        "ema_stack_h1": serialize_ema_stack(snap.get("ema_stack_h1")),  # type: ignore[arg-type]
        "cot": serialize_cot(snap.get("cot")),  # type: ignore[arg-type]
        "symbol_order": [s.base for s in config.SYMBOLS],
        "symbol_meta": {
            s.base: {
                "display_size": s.display_size,
                "round_step": s.round_step,
                # Pips-conversion meta (config-driven, connection-independent):
                # pip_price = the market pip in price units. Lets the frontend
                # render net P/L in PIPS for the live feed consistently.
                **(
                    {"pip_price": config.PIP_PRICE[s.base]}
                    if s.base in config.PIP_PRICE else {}
                ),
                # Broker-provided numeric meta (filled in once at startup) —
                # used by the frontend to compute pips/spread in broker units
                # instead of guessing from price magnitude.
                **(snap.get("broker_meta", {}) or {}).get(s.base, {}),
            }
            for s in config.SYMBOLS
        },
    }
    return out


def snapshot_light(state: LatestState) -> dict[str, Any]:
    """A price-only update: status / price / account, no heavy analysis blocks.

    Sent at the 2 Hz price cadence so the ~169 KB analysis payload is not
    re-shipped every tick. ``partial: True`` tells the client to MERGE this
    into the snapshot it already holds rather than replace it.

    Uses :meth:`LatestState.light_snapshot` (not the full ``snapshot``) so the
    ever-growing live-trigger history is not deep-copied twice a second for
    fields this payload never includes.
    """
    snap = state.light_snapshot()
    return {
        "partial": True,
        "version": int(snap["version"]),  # type: ignore[arg-type]
        "ts": float(snap["ts"]),  # type: ignore[arg-type]
        "status": serialize_status(snap["status"]),  # type: ignore[arg-type]
        "price": serialize_price(snap["price"]),  # type: ignore[arg-type]
        "account": serialize_account(snap["account"]),  # type: ignore[arg-type]
    }
