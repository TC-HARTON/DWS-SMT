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
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

import MetaTrader5 as mt5

import config
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
        )

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
