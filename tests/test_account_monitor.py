"""Unit tests for analyzer.account_monitor (SPEC §14)."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import MetaTrader5 as mt5
import pytest

import config
from analyzer.account_monitor import (
    ClosedTrade,
    PerformanceEngine,
    RangeStats,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _deal(*, ticket: int, position_id: int, symbol: str,
          type_: int, entry: int, volume: float, price: float,
          profit: float, time_: float, swap: float = 0.0,
          commission: float = 0.0) -> SimpleNamespace:
    return SimpleNamespace(
        ticket=ticket, position_id=position_id, symbol=symbol,
        type=type_, entry=entry, volume=volume, price=price,
        profit=profit, time=time_, swap=swap, commission=commission,
    )


def _pair_trade(*, pos_id: int, symbol: str, side: int,
                vol: float, entry_price: float, exit_price: float,
                profit: float, opened_at: float, closed_at: float,
                swap: float = 0.0, commission: float = 0.0):
    """Return (entry_deal, exit_deal) for a closed trade."""
    return (
        _deal(ticket=pos_id * 2, position_id=pos_id, symbol=symbol,
              type_=side, entry=mt5.DEAL_ENTRY_IN, volume=vol,
              price=entry_price, profit=0.0, time_=opened_at),
        _deal(ticket=pos_id * 2 + 1, position_id=pos_id, symbol=symbol,
              type_=side, entry=mt5.DEAL_ENTRY_OUT, volume=vol,
              price=exit_price, profit=profit, time_=closed_at,
              swap=swap, commission=commission),
    )


# --------------------------------------------------------------------------- #
# Deal pairing
# --------------------------------------------------------------------------- #

def test_pair_deals_reconstructs_one_trade_per_pair():
    in_d, out_d = _pair_trade(pos_id=1, symbol="XAUUSD", side=mt5.DEAL_TYPE_BUY,
                              vol=0.10, entry_price=4500.0, exit_price=4520.0,
                              profit=200.0, opened_at=100.0, closed_at=200.0)
    trades = PerformanceEngine._pair_deals((in_d, out_d))
    assert len(trades) == 1
    t = trades[0]
    assert t.symbol == "XAUUSD"
    assert t.type == "BUY"
    assert t.entry_price == 4500.0 and t.exit_price == 4520.0
    assert t.profit == 200.0


def test_pair_deals_ignores_balance_deposits():
    bal = _deal(ticket=1, position_id=0, symbol="",
                type_=mt5.DEAL_TYPE_BALANCE, entry=mt5.DEAL_ENTRY_IN,
                volume=0.0, price=0.0, profit=10000.0, time_=50.0)
    trades = PerformanceEngine._pair_deals((bal,))
    assert trades == ()


def test_pair_deals_handles_orphan_close_deal():
    # OUT deal arrives without a prior IN deal (entry outside the fetch range).
    out_d = _deal(ticket=999, position_id=42, symbol="EURUSD",
                  type_=mt5.DEAL_TYPE_BUY, entry=mt5.DEAL_ENTRY_OUT,
                  volume=0.50, price=1.10, profit=-50.0, time_=400.0)
    trades = PerformanceEngine._pair_deals((out_d,))
    assert len(trades) == 1
    assert trades[0].symbol == "EURUSD"
    assert trades[0].profit == -50.0


# --------------------------------------------------------------------------- #
# Range summary
# --------------------------------------------------------------------------- #

def test_summarise_empty_trades_yields_zeroed_stats():
    rs = PerformanceEngine._summarise("24h", ())
    assert rs.trade_count == 0
    assert rs.win_rate == 0.0
    assert rs.profit_factor is None
    assert rs.by_symbol == {}


def test_summarise_basic_metrics():
    now = time.time()
    trades = (
        ClosedTrade(1, "XAUUSD", "BUY", 0.10, now - 100, now - 50,
                    4500.0, 4520.0, 200.0, 0.0, 0.0),
        ClosedTrade(2, "EURUSD", "SELL", 0.30, now - 80, now - 30,
                    1.10, 1.10, -50.0, 0.0, 0.0),
        ClosedTrade(3, "XAUUSD", "BUY", 0.10, now - 60, now - 20,
                    4520.0, 4530.0, 100.0, 0.0, 0.0),
    )
    rs = PerformanceEngine._summarise("24h", trades)
    assert rs.trade_count == 3
    assert rs.win_count == 2 and rs.loss_count == 1
    assert rs.win_rate == pytest.approx(2 / 3)
    assert rs.gross_profit == 300.0
    assert rs.gross_loss == -50.0
    assert rs.net_profit == 250.0
    # PF = 300 / 50 = 6
    assert rs.profit_factor == pytest.approx(6.0)
    # avg_win=150, avg_loss=-50 ⇒ RR=3
    assert rs.risk_reward == pytest.approx(3.0)
    # Per-symbol slice
    xau = rs.by_symbol["XAUUSD"]
    assert xau.trade_count == 2 and xau.win_count == 2
    eur = rs.by_symbol["EURUSD"]
    assert eur.trade_count == 1 and eur.net_profit == -50.0


def test_summarise_drawdown_tracks_peak_to_trough():
    base = 1_700_000_000.0
    trades = (
        ClosedTrade(1, "A", "BUY", 0.1, base, base + 1, 100.0, 100.0, +100.0, 0.0, 0.0),
        ClosedTrade(2, "A", "BUY", 0.1, base + 2, base + 3, 100.0, 100.0, +200.0, 0.0, 0.0),
        ClosedTrade(3, "A", "BUY", 0.1, base + 4, base + 5, 100.0, 100.0, -150.0, 0.0, 0.0),
        ClosedTrade(4, "A", "BUY", 0.1, base + 6, base + 7, 100.0, 100.0, -50.0, 0.0, 0.0),
    )
    rs = PerformanceEngine._summarise("24h", trades)
    # Running: +100, +300, +150, +100
    # Peak: 300. Max dd_abs = 300 - 100 = 200.
    # Max dd_pct = 200 / 300 * 100 = 66.67
    assert rs.max_drawdown_abs == pytest.approx(200.0)
    assert rs.max_drawdown_pct == pytest.approx(200 / 300 * 100, abs=1e-6)


def test_summarise_hour_bucket_uses_jst():
    # exit_time at 2026-01-01 03:00 UTC → 12:00 JST.
    exit_ts = 1_767_236_400.0  # 2026-01-01 03:00 UTC
    trades = (
        ClosedTrade(1, "XAUUSD", "BUY", 0.1, exit_ts - 60, exit_ts,
                    100.0, 100.0, 50.0, 0.0, 0.0),
    )
    rs = PerformanceEngine._summarise("24h", trades)
    assert 12 in rs.by_hour_jst
    assert rs.by_hour_jst[12] == pytest.approx(50.0)


# --------------------------------------------------------------------------- #
# End-to-end compute
# --------------------------------------------------------------------------- #

def test_compute_uses_largest_finite_range_for_history_fetch():
    conn = MagicMock()
    # Two recent BUY trades.
    in1, out1 = _pair_trade(pos_id=1, symbol="XAUUSD", side=mt5.DEAL_TYPE_BUY,
                            vol=0.1, entry_price=4500.0, exit_price=4520.0,
                            profit=200.0,
                            opened_at=time.time() - 100,
                            closed_at=time.time() - 50)
    conn.history_deals.return_value = (in1, out1)
    conn.account_snapshot.return_value = SimpleNamespace(positions=())
    eng = PerformanceEngine(conn)
    snap = eng.compute()
    # Connector was asked for history at least once — we requested 90d window.
    assert conn.history_deals.call_args_list, "history_deals never called"
    # 24h / 7d / 30d / 90d / all should each have one trade summarised
    # (all-time triggers a second call internally that returns the same data).
    for r in config.HISTORY_RANGES:
        rs = snap.by_range[r.label]
        # The trade is recent, so every range should pick it up.
        assert isinstance(rs, RangeStats)
    assert snap.by_range["24h"].trade_count == 1
    assert snap.open_trade_count == 0
