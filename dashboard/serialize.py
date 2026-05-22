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

from typing import Any, Mapping

import config
from analyzer.account_monitor import PerformanceSnapshot, RangeStats, SymbolStats
from analyzer.calendar_feed import CalendarEvent, CalendarSnapshot
from analyzer.confluence import ConfluenceCluster
from analyzer.correlation import CorrelationMatrix, CorrelationSnapshot
from analyzer.currency_strength import (
    CurrencyScore,
    PairBias,
    StrengthSnapshot,
    StrengthWindowResult,
)
from analyzer.dws_smt import DwsSmtResult
from analyzer.indicator_engine import (
    AnalysisSnapshot,
    ChartBars,
    SymbolIndicators,
    TimeframeIndicators,
)
from analyzer.mt5_connector import AccountSnapshot, Tick
from analyzer.price_action import PriceActionEvent
from analyzer.state import (
    ConnectionStatus,
    LatestState,
    PriceSnapshot,
    StructuresSnapshot,
    SymbolStructures,
)
from analyzer.structure_types import StructureLevel


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


def serialize_dws_smt(d: DwsSmtResult | None) -> dict[str, Any] | None:
    """Serialise the DWS-SMT histogram as compact column arrays.

    Each base window ships parallel arrays: ``t`` = epoch-ms bar times,
    ``c`` = per-bar ``[row, ...]`` colour indices (0 up / 1 down / 2 flat),
    ``g`` = per-bar trigger ("BUY"/"SELL"/"EXIT") or ``None``, ``bias`` = the
    per-bar composite BIAS score (-10..+10, judged at that bar's own time).
    ``trades`` lists paired entry→exit trades — ``i`` entry bar, ``d`` direction
    (+1/-1), ``p`` signed price points, ``m`` max adverse excursion, ``o`` open.
    """
    if d is None:
        return None
    return {
        "by_base": {
            base: {
                "base_tf": w.base_tf,
                "rows": list(w.rows),
                "t": w.times_ms.tolist(),
                "c": w.colors.tolist(),
                "g": list(w.triggers),
                "bias": [round(float(x), 2) for x in w.bias],
                "trades": [
                    {"i": tr.entry_idx, "d": tr.direction,
                     "p": round(tr.points, 5), "m": round(tr.mae, 5),
                     "o": tr.is_open}
                    for tr in w.trades
                ],
            }
            for base, w in d.by_base.items()
        },
    }


def serialize_symbol(sym: SymbolIndicators) -> dict[str, Any]:
    return {
        "base": sym.base,
        "broker_name": sym.broker_name,
        "by_tf": {label: serialize_timeframe(tf) for label, tf in sym.by_tf.items()},
        "chart": serialize_chart(sym.chart),
        "dws": serialize_dws_smt(sym.dws),
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
# Phase 2: structures / price action / confluence
# --------------------------------------------------------------------------- #

def serialize_level(lv: StructureLevel) -> dict[str, Any]:
    return {
        "name": lv.name,
        "kind": lv.kind.value,
        "category": lv.category,
        "source": lv.source.value,
        "price": _opt_float(lv.price),
        "importance": int(lv.importance),
        "color": lv.color,
        "tf": lv.tf,
        # meta may contain nested datetimes etc.; we coerce known sub-types
        # rather than dumping arbitrary content to avoid silent json errors.
        "meta": _safe_meta(lv.meta),
    }


def serialize_price_action(ev: PriceActionEvent) -> dict[str, Any]:
    return {
        "kind": ev.kind.value,
        "bar_time": ev.bar_time.isoformat() if ev.bar_time is not None else None,
        "bar_index_from_end": int(ev.bar_index_from_end),
        "direction": int(ev.direction),
        "close": _opt_float(ev.close),
        "extreme": _opt_float(ev.extreme),
        "body": _opt_float(ev.body),
        "note": ev.note,
        "meta": _safe_meta(ev.meta),
    }


def serialize_confluence(c: ConfluenceCluster) -> dict[str, Any]:
    return {
        "center": _opt_float(c.center),
        "price_low": _opt_float(c.price_low),
        "price_high": _opt_float(c.price_high),
        "width": _opt_float(c.width),
        "distance": _opt_float(c.distance),
        "score": int(c.score),
        "importance_label": c.importance_label,
        "level_names": [lv.name for lv in c.levels],
        "level_categories": [lv.category for lv in c.levels],
        "level_sources": [lv.source.value for lv in c.levels],
    }


def serialize_symbol_structures(s: SymbolStructures) -> dict[str, Any]:
    return {
        "levels": [serialize_level(lv) for lv in s.levels],
        "price_action": [serialize_price_action(ev) for ev in s.price_action],
        "confluences": [serialize_confluence(c) for c in s.confluences],
    }


def serialize_structures(s: StructuresSnapshot | None) -> dict[str, Any] | None:
    if s is None:
        return None
    # The flattened wire shape keeps the existing per-domain keys at the top
    # level for the front end — the only change is that the backend now
    # builds them in a single loop from one source dict.
    by_sym = s.by_symbol
    return {
        "generated_at": float(s.generated_at),
        "levels_by_symbol": {
            base: [serialize_level(lv) for lv in sym.levels]
            for base, sym in by_sym.items()
        },
        "price_action_by_symbol": {
            base: [serialize_price_action(ev) for ev in sym.price_action]
            for base, sym in by_sym.items()
        },
        "confluences_by_symbol": {
            base: [serialize_confluence(c) for c in sym.confluences]
            for base, sym in by_sym.items()
        },
    }


# --------------------------------------------------------------------------- #
# Phase 3: currency strength / correlation / performance
# --------------------------------------------------------------------------- #

def serialize_score(s: CurrencyScore) -> dict[str, Any]:
    return {
        "currency": s.currency,
        "score": _opt_float(s.score),
        "raw_avg": _opt_float(s.raw_avg),
        "n_pairs": int(s.n_pairs),
        "is_reference": bool(s.is_reference),
    }


def serialize_pair_bias(b: PairBias) -> dict[str, Any]:
    return {
        "pair": b.pair, "base": b.base, "quote": b.quote,
        "delta": _opt_float(b.delta), "label": b.label,
    }


def serialize_strength_window(w: StrengthWindowResult) -> dict[str, Any]:
    return {
        "window": w.window,
        "scores": {ccy: serialize_score(sc) for ccy, sc in w.scores.items()},
        "pair_biases": {p: serialize_pair_bias(b) for p, b in w.pair_biases.items()},
    }


def serialize_strength(s: StrengthSnapshot | None) -> dict[str, Any] | None:
    if s is None:
        return None
    return {
        "generated_at": float(s.generated_at),
        "compute_ms": float(s.compute_ms),
        "default_window": config.STRENGTH_DEFAULT_WINDOW,
        "windows": [w.label for w in config.STRENGTH_WINDOWS],
        "display_currencies": list(config.FIAT_CURRENCIES),
        "by_window": {
            label: serialize_strength_window(w) for label, w in s.by_window.items()
        },
    }


def serialize_correlation_matrix(m: CorrelationMatrix) -> dict[str, Any]:
    # numpy.ndarray → list-of-lists; round to 3 dp to keep the WS payload
    # small (10x10 = 100 floats per window × 3 windows = 300 floats, but
    # at 3 dp they compress nicely).
    mat = m.matrix.tolist() if hasattr(m.matrix, "tolist") else m.matrix
    return {
        "bars": int(m.bars),
        "symbols": list(m.symbols),
        "matrix": [[round(_opt_float(v) if v is not None else 0.0, 4)
                    for v in row]
                   for row in mat],
    }


def serialize_correlation(s: CorrelationSnapshot | None) -> dict[str, Any] | None:
    if s is None:
        return None
    return {
        "generated_at": float(s.generated_at),
        "compute_ms": float(s.compute_ms),
        "timeframe": s.timeframe,
        "default_window": config.CORRELATION_DEFAULT_BARS,
        "windows": list(config.CORRELATION_WINDOWS_BARS),
        "by_window": {
            str(bars): serialize_correlation_matrix(m)
            for bars, m in s.by_window.items()
        },
    }


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
    }


def serialize_calendar_event(e: CalendarEvent) -> dict[str, Any]:
    return {
        "release_ts": float(e.release_ts),
        "currency": e.currency,
        "title": e.title,
        "impact": e.impact,
        "forecast": e.forecast,
        "previous": e.previous,
        "actual": e.actual,
        "source": e.source,
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


def _safe_meta(
    meta: dict[str, Any], _visited: set[int] | None = None
) -> dict[str, Any]:
    """Best-effort JSON-safe coercion for level/PA meta dicts.

    A ``_visited`` set guards against self-referential or cyclic dicts; the
    EA parser is defensive but a hand-crafted ``lines_*.json`` could in
    theory smuggle one in, and infinite recursion here would crash the WS
    broadcaster for every connected client.
    """
    if _visited is None:
        _visited = set()
    out: dict[str, Any] = {}
    for k, v in meta.items():
        if v is None or isinstance(v, (bool, int, str)):
            out[k] = v
        elif isinstance(v, float):
            out[k] = _opt_float(v)
        elif isinstance(v, (list, tuple)):
            out[k] = list(v)  # ISO-time strings & numbers pass through
        elif isinstance(v, dict):
            if id(v) in _visited:
                out[k] = "<cycle>"
                continue
            _visited.add(id(v))
            out[k] = _safe_meta(v, _visited)
            _visited.discard(id(v))
        else:
            out[k] = str(v)
    return out


def snapshot_to_json(state: LatestState) -> dict[str, Any]:
    """Render the entire :class:`LatestState` into a flat JSON-ready dict."""
    snap = state.snapshot()
    return {
        "version": int(snap["version"]),  # type: ignore[arg-type]
        "ts": float(snap["ts"]),  # type: ignore[arg-type]
        "status": serialize_status(snap["status"]),  # type: ignore[arg-type]
        "price": serialize_price(snap["price"]),  # type: ignore[arg-type]
        "analysis": serialize_analysis(snap["analysis"]),  # type: ignore[arg-type]
        "account": serialize_account(snap["account"]),  # type: ignore[arg-type]
        "structures": serialize_structures(snap["structures"]),  # type: ignore[arg-type]
        "strength": serialize_strength(snap["strength"]),  # type: ignore[arg-type]
        "correlation": serialize_correlation(snap["correlation"]),  # type: ignore[arg-type]
        "performance": serialize_performance(snap["performance"]),  # type: ignore[arg-type]
        "calendar": serialize_calendar(snap["calendar"]),  # type: ignore[arg-type]
        "symbol_order": [s.base for s in config.SYMBOLS],
        "symbol_meta": {
            s.base: {
                "display_size": s.display_size,
                "round_step": s.round_step,
                # Broker-provided numeric meta (filled in once at startup) —
                # used by the frontend to compute pips/spread in broker units
                # instead of guessing from price magnitude.
                **(snap.get("broker_meta", {}) or {}).get(s.base, {}),
            }
            for s in config.SYMBOLS
        },
    }


def snapshot_light(state: LatestState) -> dict[str, Any]:
    """A price-only update: status / price / account, no heavy analysis blocks.

    Sent at the 2 Hz price cadence so the ~169 KB analysis payload is not
    re-shipped every tick. ``partial: True`` tells the client to MERGE this
    into the snapshot it already holds rather than replace it.
    """
    snap = state.snapshot()
    return {
        "partial": True,
        "version": int(snap["version"]),  # type: ignore[arg-type]
        "ts": float(snap["ts"]),  # type: ignore[arg-type]
        "status": serialize_status(snap["status"]),  # type: ignore[arg-type]
        "price": serialize_price(snap["price"]),  # type: ignore[arg-type]
        "account": serialize_account(snap["account"]),  # type: ignore[arg-type]
    }


def jsonable(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Pass-through for things that are already pure dicts (kept for symmetry)."""
    return dict(mapping)
