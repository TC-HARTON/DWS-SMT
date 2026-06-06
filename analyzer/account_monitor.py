"""SPEC §14.2 + §14.3 trade history retrieval & performance analysis.

We pull the maximum-spanning range (90 d in practice; SPEC §14.2 also
permits "全期間" but that can balloon to thousands of deals and pushes
the 60 s budget hard) once per :data:`config.HISTORY_REFRESH_SEC` and
then derive per-range performance metrics in memory. The UI picks a
range from the precomputed bundle so the user can toggle 24h / 7d / 30d
/ 90d without waiting for a new fetch.

MT5 deal records (``mt5.TradeDeal``) come in pairs — DEAL_ENTRY_IN
opens a position; DEAL_ENTRY_OUT (or _IN_OUT for partial close) closes
it and carries the realised profit. We pair them by ``position_id`` so
each closed trade becomes one ``ClosedTrade`` record we can then
aggregate.
"""

from __future__ import annotations

import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Iterable

import MetaTrader5 as mt5
import numpy as np

import config
from analyzer import journal_store
from analyzer.mt5_connector import MT5Connector

log = logging.getLogger(__name__)

# Upper bound when the user asks for "all-time" history. MT5 may not return
# anything beyond what the broker keeps; 5 years is a safe ceiling that
# avoids overwhelming the IPC channel for a long-running demo account.
ALL_TIME_FETCH_DAYS: int = 5 * 365


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ClosedTrade:
    """One round-trip (entry + exit) reconstructed from MT5 deals."""

    position_id: int
    symbol: str
    type: str                 # "BUY" or "SELL"
    volume: float             # absolute lots closed
    entry_time: float         # epoch seconds
    exit_time: float          # epoch seconds
    entry_price: float
    exit_price: float
    profit: float             # realised net (already includes commission/swap on the close deal)
    swap: float
    commission: float
    # --- analysis enrichments (defaults = not yet computed) ---
    pips: float = 0.0
    mae_pips: float | None = None
    mfe_pips: float | None = None
    r_multiple: float | None = None
    sl: float | None = None
    tp: float | None = None
    ctx: dict = field(default_factory=dict)
    env: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RangeStats:
    """SPEC §14.3 performance metrics for a single range window."""

    range_label: str          # "24h" | "7d" | "30d" | "90d" | "all"
    trade_count: int
    win_count: int
    loss_count: int
    win_rate: float           # 0..1
    profit_factor: float | None   # gross_win / gross_loss; None if no losses
    max_drawdown_abs: float   # peak-to-trough running PnL
    max_drawdown_pct: float   # as % of running peak (0 if peak<=0)
    avg_win: float
    avg_loss: float           # negative number
    risk_reward: float | None # avg_win / |avg_loss|; None if no losses
    gross_profit: float
    gross_loss: float         # negative number
    net_profit: float
    by_symbol: dict[str, "SymbolStats"]
    by_hour_jst: dict[int, float]  # JST hour-of-day → net pnl


@dataclass(frozen=True)
class SymbolStats:
    """Per-symbol slice of the range stats (SPEC §14.3 銘柄別損益)."""

    symbol: str
    trade_count: int
    win_count: int
    net_profit: float
    win_rate: float


@dataclass(frozen=True)
class AdvancedStats:
    """spec §6.2-6.4 risk-adjusted metrics + curves derived from one window."""

    sharpe: float | None
    sortino: float | None
    calmar: float | None
    recovery_factor: float | None
    ulcer_index: float | None
    var_95: float | None
    cvar_95: float | None
    max_win_streak: int
    max_loss_streak: int
    current_streak: int
    max_drawdown_abs: float
    underwater_pct: float
    r_distribution: dict[str, int]
    equity_curve: list[float]
    underwater_curve: list[float]


@dataclass(frozen=True)
class EdgeStats:
    """spec §7 per-condition WR/PF. Each map: bucket-label → {n,win_rate,pf}."""

    by_alignment: dict[str, dict]
    by_adx: dict[str, dict]
    by_rsi: dict[str, dict]
    by_weekday_jst: dict[str, dict]
    by_hold_min: dict[str, dict]
    by_dxy: dict[str, dict]
    by_cot_extreme: dict[str, dict]
    by_real_yield: dict[str, dict]
    by_flip: dict[str, dict]


@dataclass(frozen=True)
class PerformanceSnapshot:
    """Container returned by :meth:`PerformanceEngine.compute`."""

    generated_at: float       # epoch seconds (UTC)
    compute_ms: float
    fetched_from_ts: float    # epoch seconds — lower bound of pulled deals
    fetched_to_ts: float
    by_range: dict[str, RangeStats]
    open_trade_count: int     # snapshot of currently open positions for context
    # SPEC §14.1 当日P&L(算出): realised closes since UTC midnight + the
    # current floating profit on every still-open position.
    today_realised_pnl: float
    today_floating_pnl: float
    today_total_pnl: float
    # spec §5/§6/§7 — default-range detail bundle (frontend renders these)
    trades: tuple["ClosedTrade", ...] = ()
    advanced: "AdvancedStats | None" = None
    edge: "EdgeStats | None" = None


# --------------------------------------------------------------------------- #
# Pip / R helpers
# --------------------------------------------------------------------------- #

def _range_days(label: str) -> int:
    """HISTORY_RANGES のラベル→日数(該当なしは 90)。"""
    for r in config.HISTORY_RANGES:
        if r.label == label and r.days is not None:
            return r.days
    return 90


def _pip_price(symbol: str) -> float:
    """Pip size in PRICE units for *symbol* (XAUUSD pip = 0.10)."""
    return config.PIP_PRICE.get(symbol, config.PIP_PRICE.get("XAUUSD", 0.10))


def trade_pips(symbol: str, side: str, entry: float, exit_: float) -> float:
    """Signed realised move in pips. BUY profits when exit>entry; SELL inverse."""
    raw = (exit_ - entry) if side == "BUY" else (entry - exit_)
    return raw / _pip_price(symbol)


def r_multiple(symbol: str, side: str, entry: float, exit_: float,
               sl: float | None) -> float | None:
    """Realised R = realised_pips / risk_pips. None if no usable SL."""
    if sl is None:
        return None
    risk = abs(entry - sl) / _pip_price(symbol)
    if risk <= 0.0:
        return None
    return trade_pips(symbol, side, entry, exit_) / risk


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class PerformanceEngine:
    """Fetch trade history once + derive per-range stats from one buffer."""

    def __init__(
        self,
        connector: MT5Connector,
        ranges: Iterable[config.HistoryRange] = config.HISTORY_RANGES,
    ) -> None:
        self._connector = connector
        self._ranges = tuple(ranges)
        self._mae_mfe_cache: dict[int, tuple[float | None, float | None]] = {}

    # --------------------------------------------------------- compute
    def compute(self) -> PerformanceSnapshot:
        t0 = time.perf_counter()
        now = time.time()
        wants_alltime = any(r.days is None for r in self._ranges)
        max_days = max((r.days for r in self._ranges if r.days is not None),
                       default=90)
        # If "all" is requested, pull 5 years up-front and slice every range
        # off that single buffer. This halves the IPC round-trips per cycle
        # vs the previous double-fetch (90 d + 5 y).
        fetch_days = ALL_TIME_FETCH_DAYS if wants_alltime else max_days
        from_ts = now - fetch_days * 86400.0
        deals = self._connector.history_deals(from_ts, now)
        all_trades = self._pair_deals(deals)
        # Cap finite-range trades to ``max_days`` from a single source.
        max_cutoff = now - max_days * 86400.0
        finite_trades = tuple(t for t in all_trades if t.exit_time >= max_cutoff)

        # Snapshot current open positions for the context badge.
        open_count = 0
        floating_pnl = 0.0
        acc = self._connector.account_snapshot()
        if acc is not None:
            open_count = len(acc.positions)
            floating_pnl = sum(p.profit + p.swap for p in acc.positions)

        # SPEC §14.1 当日P&L: realised closes since today's UTC midnight.
        today_utc_midnight = datetime.fromtimestamp(now, tz=timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0,
        ).timestamp()
        today_realised = sum(
            (t.profit + t.swap + t.commission)
            for t in finite_trades if t.exit_time >= today_utc_midnight
        )

        by_range: dict[str, RangeStats] = {}
        for rng in self._ranges:
            if rng.days is None:
                window_trades = all_trades
            else:
                cutoff = now - rng.days * 86400.0
                window_trades = tuple(t for t in all_trades if t.exit_time >= cutoff)
            by_range[rng.label] = self._summarise(rng.label, window_trades)

        # spec §5-7: enrich the default-range window with journal context +
        # per-trade metrics, then derive advanced + edge bundles from it.
        server = getattr(acc, "server", None) if acc is not None else None
        journal = journal_store.load_recent(server, limit=2000)
        default_cutoff = now - _range_days(config.HISTORY_DEFAULT_RANGE) * 86400.0
        default_trades = tuple(t for t in all_trades if t.exit_time >= default_cutoff)
        enriched = self._enrich_trades(default_trades, journal)
        advanced = self._advanced_stats(enriched)
        edge = self._edge_stats(enriched)

        # MAE/MFE キャッシュを現 default-range の取引に絞る。エンジンはプロセス寿命の
        # シングルトンなので、窓外へ出た古い position のキャッシュを破棄して無制限増加を防ぐ。
        live_pids = {t.position_id for t in enriched}
        self._mae_mfe_cache = {
            pid: v for pid, v in self._mae_mfe_cache.items() if pid in live_pids
        }

        return PerformanceSnapshot(
            generated_at=now,
            compute_ms=(time.perf_counter() - t0) * 1000.0,
            fetched_from_ts=from_ts,
            fetched_to_ts=now,
            by_range=by_range,
            open_trade_count=open_count,
            today_realised_pnl=today_realised,
            today_floating_pnl=floating_pnl,
            today_total_pnl=today_realised + floating_pnl,
            trades=enriched,
            advanced=advanced,
            edge=edge,
        )

    # ------------------------------------------------- MAE/MFE
    def _mae_mfe(self, symbol: str, side: str, entry: float,
                 from_ts: float, to_ts: float) -> tuple[float | None, float | None]:
        """Max adverse / favourable excursion in pips over the hold window.

        Confirmed bars only (look-ahead safe). ``(None, None)`` when no bars.
        """
        try:
            bars = self._connector.copy_rates_range(
                symbol, mt5.TIMEFRAME_M1, from_ts, to_ts)
        except Exception:  # noqa: BLE001 — MAE/MFE is best-effort, never fatal
            log.exception("copy_rates_range failed for MAE/MFE")
            return (None, None)
        if bars is None or len(bars) == 0:
            return (None, None)
        hi = float(bars["high"].max())
        lo = float(bars["low"].min())
        pip = _pip_price(symbol)
        if side == "BUY":
            mfe = (hi - entry) / pip
            mae = (lo - entry) / pip
        else:
            mfe = (entry - lo) / pip
            mae = (entry - hi) / pip
        return (mae, mfe)

    def _mae_mfe_cached(self, position_id: int, symbol: str, side: str,
                        entry: float, from_ts: float, to_ts: float,
                        ) -> tuple[float | None, float | None]:
        """``_mae_mfe`` memoised by position_id (one IPC fetch per trade)."""
        hit = self._mae_mfe_cache.get(position_id)
        if hit is not None:
            return hit
        val = self._mae_mfe(symbol, side, entry, from_ts, to_ts)
        self._mae_mfe_cache[position_id] = val
        return val

    # ------------------------------------------------- deal pairing
    @staticmethod
    def _pair_deals(deals: tuple) -> tuple[ClosedTrade, ...]:
        """Reconstruct closed round-trips by pairing IN/OUT deals.

        MT5 ``TradeDeal.entry`` semantics:
          * ``DEAL_ENTRY_IN``  → entry
          * ``DEAL_ENTRY_OUT`` → close
          * ``DEAL_ENTRY_INOUT`` → reversal (treated as close-then-open)

        We ignore balance/credit deals (``type == DEAL_TYPE_BALANCE``).
        """
        # Bucket by position_id; emit one ClosedTrade per OUT deal.
        opens_by_pos: dict[int, list] = defaultdict(list)
        out: list[ClosedTrade] = []

        for d in deals:
            if getattr(d, "type", None) not in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL):
                continue
            pos_id = getattr(d, "position_id", 0)
            entry = getattr(d, "entry", None)
            if entry == mt5.DEAL_ENTRY_IN:
                opens_by_pos[pos_id].append(d)
            elif entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT):
                open_deals = opens_by_pos.get(pos_id) or []
                if not open_deals:
                    # Orphan close (entry deal is older than the fetch range).
                    open_d = None
                else:
                    # Consume the earliest matching IN so multi-IN positions
                    # and partial scale-out exits pair correctly. Without
                    # popping, every OUT against the same position_id would
                    # reuse the first IN and report stale entry price/time.
                    open_d = open_deals.pop(0)
                trade_type = "BUY" if (open_d and open_d.type == mt5.DEAL_TYPE_BUY) \
                                   else "SELL" if open_d else (
                                       "SELL" if d.type == mt5.DEAL_TYPE_BUY else "BUY"
                                   )
                out.append(ClosedTrade(
                    position_id=pos_id,
                    symbol=d.symbol,
                    type=trade_type,
                    volume=float(d.volume),
                    entry_time=float(open_d.time) if open_d else float(d.time),
                    exit_time=float(d.time),
                    entry_price=float(open_d.price) if open_d else float(d.price),
                    exit_price=float(d.price),
                    profit=float(d.profit),
                    swap=float(getattr(d, "swap", 0.0)),
                    commission=float(getattr(d, "commission", 0.0)),
                ))
        return tuple(out)

    # ------------------------------------------------- per-range stats
    @staticmethod
    def _summarise(label: str, trades: tuple[ClosedTrade, ...]) -> RangeStats:
        if not trades:
            return RangeStats(
                range_label=label, trade_count=0, win_count=0, loss_count=0,
                win_rate=0.0, profit_factor=None,
                max_drawdown_abs=0.0, max_drawdown_pct=0.0,
                avg_win=0.0, avg_loss=0.0, risk_reward=None,
                gross_profit=0.0, gross_loss=0.0, net_profit=0.0,
                by_symbol={}, by_hour_jst={},
            )

        # Net PnL = exchange profit + swap + commission (commission is
        # already negative for fees on most brokers).
        pnls: list[float] = []
        for t in trades:
            pnls.append(t.profit + t.swap + t.commission)
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_profit = sum(wins)
        gross_loss = sum(losses)
        net = gross_profit + gross_loss

        win_count = len(wins)
        loss_count = len(losses)
        win_rate = win_count / len(trades) if trades else 0.0
        profit_factor = (gross_profit / abs(gross_loss)) if gross_loss < 0 else None
        avg_win = (sum(wins) / win_count) if win_count else 0.0
        avg_loss = (sum(losses) / loss_count) if loss_count else 0.0
        risk_reward = (avg_win / abs(avg_loss)) if loss_count else None

        # Drawdown over the chronological PnL curve.
        sorted_trades = sorted(trades, key=lambda t: t.exit_time)
        running = 0.0
        peak = 0.0
        max_dd_abs = 0.0
        max_dd_pct = 0.0
        for t in sorted_trades:
            running += t.profit + t.swap + t.commission
            peak = max(peak, running)
            dd = peak - running
            if dd > max_dd_abs:
                max_dd_abs = dd
                max_dd_pct = (dd / peak * 100.0) if peak > 0 else 0.0

        # Per-symbol slice.
        by_symbol_raw: dict[str, list[float]] = defaultdict(list)
        for i, t in enumerate(trades):
            by_symbol_raw[t.symbol].append(pnls[i])
        by_symbol: dict[str, SymbolStats] = {}
        for sym, vals in by_symbol_raw.items():
            sym_wins = sum(1 for v in vals if v > 0)
            by_symbol[sym] = SymbolStats(
                symbol=sym, trade_count=len(vals),
                win_count=sym_wins, net_profit=sum(vals),
                win_rate=(sym_wins / len(vals)) if vals else 0.0,
            )

        # Net PnL by JST hour of close (SPEC §14.3 "時間帯別損益").
        by_hour: dict[int, float] = defaultdict(float)
        for i, t in enumerate(trades):
            jst = datetime.fromtimestamp(t.exit_time, tz=timezone.utc)
            # JST = UTC+9, no DST.
            hour = (jst.hour + 9) % 24
            by_hour[hour] += pnls[i]

        return RangeStats(
            range_label=label,
            trade_count=len(trades),
            win_count=win_count, loss_count=loss_count,
            win_rate=win_rate,
            profit_factor=profit_factor,
            max_drawdown_abs=max_dd_abs,
            max_drawdown_pct=max_dd_pct,
            avg_win=avg_win, avg_loss=avg_loss,
            risk_reward=risk_reward,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
            net_profit=net,
            by_symbol=by_symbol,
            by_hour_jst=dict(by_hour),
        )

    # ------------------------------------------------- journal enrichment
    @staticmethod
    def _index_journal(journal: list[dict]) -> dict[int, dict]:
        """journal を ticket → entry に索引化(最新 ts 優先)。"""
        by_ticket: dict[int, dict] = {}
        for j in journal:
            tk = j.get("ticket")
            if tk is None:
                continue
            prev = by_ticket.get(int(tk))
            if prev is None or j.get("ts", 0) >= prev.get("ts", 0):
                by_ticket[int(tk)] = j
        return by_ticket

    def _enrich_trades(self, trades: tuple[ClosedTrade, ...],
                       journal: list[dict]) -> tuple[ClosedTrade, ...]:
        """Fill pips/R/MAE/MFE + journal ctx/sl/tp/env on every trade."""
        by_ticket = self._index_journal(journal)
        out: list[ClosedTrade] = []
        for t in trades:
            j = by_ticket.get(t.position_id, {})
            sl = j.get("sl")
            tp = j.get("tp")
            pips = trade_pips(t.symbol, t.type, t.entry_price, t.exit_price)
            r = r_multiple(t.symbol, t.type, t.entry_price, t.exit_price, sl)
            mae, mfe = self._mae_mfe_cached(
                t.position_id, t.symbol, t.type, t.entry_price,
                t.entry_time, t.exit_time)
            out.append(replace(
                t, pips=pips, r_multiple=r, mae_pips=mae, mfe_pips=mfe,
                sl=sl, tp=tp, ctx=dict(j.get("ctx") or {}),
                env=dict(j.get("env") or {}),
            ))
        return tuple(out)

    # ------------------------------------------------- advanced stats (spec §6.2-6.4)
    @staticmethod
    def _bin_label(value: float, edges: tuple[float, ...]) -> str:
        """value を内側境界 edges(昇順)で人間可読なビンラベルに割り当てる。"""
        import bisect
        i = bisect.bisect_right(edges, value)
        if i == 0:
            return f"<{edges[0]:g}"
        if i == len(edges):
            return f">={edges[-1]:g}"
        return f"{edges[i-1]:g}~{edges[i]:g}"

    def _advanced_stats(self, trades: tuple[ClosedTrade, ...]) -> "AdvancedStats":
        """spec §6.2-6.4: リスク調整済み指標 + エクイティ曲線を1ウィンドウ分算出する。"""
        if not trades:
            return AdvancedStats(
                sharpe=None, sortino=None, calmar=None, recovery_factor=None,
                ulcer_index=None, var_95=None, cvar_95=None,
                max_win_streak=0, max_loss_streak=0, current_streak=0,
                max_drawdown_abs=0.0, underwater_pct=0.0,
                r_distribution={}, equity_curve=[], underwater_curve=[],
            )
        chrono = sorted(trades, key=lambda t: t.exit_time)
        pnls = np.array([t.profit + t.swap + t.commission for t in chrono],
                        dtype=float)

        equity = np.cumsum(pnls)
        peak = np.maximum.accumulate(equity)
        underwater = equity - peak
        max_dd_abs = float(-underwater.min()) if underwater.size else 0.0
        underwater_pct = float((underwater < 0).mean()) if underwater.size else 0.0

        # 連勝/連敗ストリーク計算
        max_win = max_loss = cur = 0
        for p in pnls:
            if p > 0:
                cur = cur + 1 if cur > 0 else 1
                max_win = max(max_win, cur)
            elif p < 0:
                cur = cur - 1 if cur < 0 else -1
                max_loss = max(max_loss, -cur)
            else:
                cur = 0
        current_streak = cur

        # リスク調整済み指標
        mean = float(pnls.mean())
        std = float(pnls.std(ddof=1)) if pnls.size > 1 else 0.0
        sharpe = (mean / std * math.sqrt(len(pnls))) if std > 0 else None
        downside = pnls[pnls < 0]
        dstd = float(downside.std(ddof=1)) if downside.size > 1 else 0.0
        sortino = (mean / dstd * math.sqrt(len(pnls))) if dstd > 0 else None

        net = float(equity[-1])
        calmar = (net / max_dd_abs) if max_dd_abs > 0 else None
        recovery_factor = calmar
        dd_pct = np.where(peak > 0, (peak - equity) / peak * 100.0, 0.0)
        ulcer = float(np.sqrt(np.mean(dd_pct ** 2))) if dd_pct.size else None

        var_95 = float(np.percentile(pnls, 5))
        tail = pnls[pnls <= var_95]
        cvar_95 = float(tail.mean()) if tail.size else var_95

        # R倍数分布(ビン集計)
        r_dist: dict[str, int] = {}
        for t in chrono:
            if t.r_multiple is None:
                continue
            lbl = self._bin_label(t.r_multiple, config.R_MULTIPLE_BINS)
            r_dist[lbl] = r_dist.get(lbl, 0) + 1

        return AdvancedStats(
            sharpe=sharpe, sortino=sortino, calmar=calmar,
            recovery_factor=recovery_factor, ulcer_index=ulcer,
            var_95=var_95, cvar_95=cvar_95,
            max_win_streak=max_win, max_loss_streak=max_loss,
            current_streak=current_streak,
            max_drawdown_abs=max_dd_abs, underwater_pct=underwater_pct,
            r_distribution=r_dist,
            equity_curve=[float(x) for x in equity],
            underwater_curve=[float(x) for x in underwater],
        )

    # ------------------------------------------------- edge stats (spec §7)
    @staticmethod
    def _bucket(rows: list[float]) -> dict:
        """1バケツ内の取引PnLリストから {n, win_rate, pf} を作る。"""
        n = len(rows)
        if n == 0:
            return {"n": 0, "win_rate": 0.0, "pf": None}
        wins = [p for p in rows if p > 0]
        gl = sum(p for p in rows if p < 0)
        pf = (sum(wins) / abs(gl)) if gl < 0 else None
        return {"n": n, "win_rate": len(wins) / n, "pf": pf}

    def _edge_stats(self, trades: tuple["ClosedTrade", ...]) -> "EdgeStats":
        align: dict[str, list] = defaultdict(list)
        adx: dict[str, list] = defaultdict(list)
        rsi: dict[str, list] = defaultdict(list)
        wday: dict[str, list] = defaultdict(list)
        hold: dict[str, list] = defaultdict(list)
        dxy: dict[str, list] = defaultdict(list)
        cot: dict[str, list] = defaultdict(list)
        ry: dict[str, list] = defaultdict(list)
        flip: dict[str, list] = defaultdict(list)

        for t in trades:
            pnl = t.profit + t.swap + t.commission
            for tf, c in (t.ctx or {}).items():
                if not isinstance(c, dict):
                    continue
                side = "above" if c.get("ae") else "below"
                align[f"{tf}_{side}"].append(pnl)
                if c.get("adx") is not None:
                    adx[self._bin_label(float(c["adx"]), config.EDGE_ADX_BINS)].append(pnl)
                if c.get("rsi") is not None:
                    rsi[self._bin_label(float(c["rsi"]), config.EDGE_RSI_BINS)].append(pnl)
            # 曜日(JST = UTC+9)+ 保有時間ビン
            jst = datetime.fromtimestamp(t.entry_time + 9 * 3600, tz=timezone.utc)
            wd = ["月", "火", "水", "木", "金", "土", "日"][jst.weekday()]
            wday[wd].append(pnl)
            hold_min = max(0.0, (t.exit_time - t.entry_time) / 60.0)
            hold[self._bin_label(hold_min, config.EDGE_HOLD_MIN_BINS)].append(pnl)
            # env 軸(蓄積待ち — journal 拡張後の取引のみ env を持つ)
            env = t.env or {}
            if env.get("dxy_change") is not None:
                dxy["up" if float(env["dxy_change"]) >= 0 else "down"].append(pnl)
            if env.get("cot_pctile") is not None:
                p = float(env["cot_pctile"])
                cot["low" if p < 10 else "high" if p > 90 else "mid"].append(pnl)
            if env.get("real_yield_change") is not None:
                ry["up" if float(env["real_yield_change"]) >= 0 else "down"].append(pnl)
            if env.get("flip_norm") is not None:
                a = abs(float(env["flip_norm"]))
                flip["<0.25" if a < 0.25 else "0.25~0.5" if a < 0.5 else ">=0.5"].append(pnl)

        def _fold(d: dict[str, list]) -> dict[str, dict]:
            return {k: self._bucket(v) for k, v in d.items()}

        return EdgeStats(
            by_alignment=_fold(align), by_adx=_fold(adx), by_rsi=_fold(rsi),
            by_weekday_jst=_fold(wday), by_hold_min=_fold(hold),
            by_dxy=_fold(dxy), by_cot_extreme=_fold(cot),
            by_real_yield=_fold(ry), by_flip=_fold(flip),
        )
