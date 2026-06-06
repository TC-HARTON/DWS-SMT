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


def test_config_has_risk_and_range_constants():
    import config
    # リスク上限
    assert config.DAILY_LOSS_LIMIT_PCT == -3.0
    assert config.MAX_DD_LIMIT_PCT == -10.0
    # range 拡張(既定 90d + 180d/1Y 追加)
    assert config.HISTORY_DEFAULT_RANGE == "90d"
    labels = [r.label for r in config.HISTORY_RANGES]
    assert labels == ["24h", "7d", "30d", "90d", "180d", "1Y", "all"]
    # R倍数分布 bin 境界(昇順・両端 ±inf を含まない内側境界)
    assert config.R_MULTIPLE_BINS == (-3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0, 4.0)
    # Edge bin 境界
    assert config.EDGE_ADX_BINS == (20.0, 25.0, 30.0)
    assert config.EDGE_RSI_BINS == (30.0, 50.0, 70.0)
    assert config.EDGE_HOLD_MIN_BINS == (15.0, 60.0, 240.0)


# --------------------------------------------------------------------------- #
# Sub-task 3: ClosedTrade optional analysis fields
# --------------------------------------------------------------------------- #

def test_closedtrade_has_optional_analysis_fields():
    from analyzer.account_monitor import ClosedTrade
    t = ClosedTrade(
        position_id=1, symbol="XAUUSD", type="BUY", volume=0.1,
        entry_time=100.0, exit_time=200.0, entry_price=4500.0,
        exit_price=4520.0, profit=200.0, swap=0.0, commission=0.0,
    )
    assert t.mae_pips is None
    assert t.mfe_pips is None
    assert t.r_multiple is None
    assert t.pips == 0.0
    assert t.ctx == {}
    assert t.sl is None and t.tp is None


# --------------------------------------------------------------------------- #
# Sub-task 4: pure pips + R-multiple helpers
# --------------------------------------------------------------------------- #

def test_trade_pips_and_r_multiple():
    from analyzer.account_monitor import trade_pips, r_multiple
    assert trade_pips("XAUUSD", "BUY", 4500.0, 4520.0) == pytest.approx(200.0)
    assert trade_pips("XAUUSD", "SELL", 4520.0, 4500.0) == pytest.approx(200.0)
    assert trade_pips("XAUUSD", "SELL", 4500.0, 4520.0) == pytest.approx(-200.0)
    assert r_multiple("XAUUSD", "BUY", 4500.0, 4520.0, 4490.0) == pytest.approx(2.0)
    assert r_multiple("XAUUSD", "BUY", 4500.0, 4520.0, None) is None
    assert r_multiple("XAUUSD", "BUY", 4500.0, 4520.0, 4500.0) is None


# --------------------------------------------------------------------------- #
# Sub-task 5: MAE/MFE from bars + position_id cache
# --------------------------------------------------------------------------- #

def test_mae_mfe_from_bars_buy_and_sell():
    import pandas as pd
    from analyzer.account_monitor import PerformanceEngine
    conn = MagicMock(); eng = PerformanceEngine(conn)
    bars = pd.DataFrame({"open": [4500, 4505], "high": [4512, 4508],
                         "low": [4490, 4496], "close": [4505, 4500]})
    conn.copy_rates_range.return_value = bars
    mae, mfe = eng._mae_mfe("XAUUSD", "BUY", 4500.0, 100.0, 200.0)
    assert mfe == pytest.approx(120.0)
    assert mae == pytest.approx(-100.0)
    mae_s, mfe_s = eng._mae_mfe("XAUUSD", "SELL", 4500.0, 100.0, 200.0)
    assert mfe_s == pytest.approx(100.0)
    assert mae_s == pytest.approx(-120.0)


def test_mae_mfe_empty_bars_returns_none():
    import pandas as pd
    from analyzer.account_monitor import PerformanceEngine
    conn = MagicMock()
    conn.copy_rates_range.return_value = pd.DataFrame(columns=["open","high","low","close"])
    eng = PerformanceEngine(conn)
    assert eng._mae_mfe("XAUUSD", "BUY", 4500.0, 100.0, 200.0) == (None, None)


def test_mae_mfe_cached_by_position():
    import pandas as pd
    from analyzer.account_monitor import PerformanceEngine
    conn = MagicMock()
    conn.copy_rates_range.return_value = pd.DataFrame({"high": [4510], "low": [4495]})
    eng = PerformanceEngine(conn)
    eng._mae_mfe_cached(7, "XAUUSD", "BUY", 4500.0, 100.0, 200.0)
    eng._mae_mfe_cached(7, "XAUUSD", "BUY", 4500.0, 100.0, 200.0)
    assert conn.copy_rates_range.call_count == 1


# --------------------------------------------------------------------------- #
# Sub-task 6: journal join — enrich trades with context + metrics
# --------------------------------------------------------------------------- #

def test_enrich_trades_merges_journal_and_metrics():
    from analyzer.account_monitor import PerformanceEngine, ClosedTrade
    import pandas as pd
    conn = MagicMock()
    conn.copy_rates_range.return_value = pd.DataFrame({"high": [4525.0], "low": [4498.0]})
    eng = PerformanceEngine(conn)
    raw = ClosedTrade(position_id=42, symbol="XAUUSD", type="BUY", volume=0.1,
                      entry_time=100.0, exit_time=200.0, entry_price=4500.0,
                      exit_price=4520.0, profit=200.0, swap=0.0, commission=0.0)
    journal = [{"ts": 100_000, "ticket": 42, "side": "BUY", "sl": 4490.0,
                "tp": 4560.0, "ctx": {"M15": {"ae": True}}, "env": {"dxy": 99.4}}]
    out = eng._enrich_trades((raw,), journal)
    e = out[0]
    assert e.pips == pytest.approx(200.0)
    assert e.r_multiple == pytest.approx(2.0)
    assert e.sl == 4490.0 and e.tp == 4560.0
    assert e.ctx == {"M15": {"ae": True}}
    assert e.env == {"dxy": 99.4}
    assert e.mfe_pips == pytest.approx(250.0)
    assert e.mae_pips == pytest.approx(-20.0)


def test_enrich_trades_without_journal_match():
    from analyzer.account_monitor import PerformanceEngine, ClosedTrade
    import pandas as pd
    conn = MagicMock()
    conn.copy_rates_range.return_value = pd.DataFrame(columns=["high","low"])
    eng = PerformanceEngine(conn)
    raw = ClosedTrade(position_id=1, symbol="XAUUSD", type="SELL", volume=0.1,
                      entry_time=100.0, exit_time=200.0, entry_price=4520.0,
                      exit_price=4500.0, profit=200.0, swap=0.0, commission=0.0)
    out = eng._enrich_trades((raw,), [])
    e = out[0]
    assert e.pips == pytest.approx(200.0)
    assert e.r_multiple is None
    assert e.sl is None and e.ctx == {}
    assert e.mae_pips is None


# --------------------------------------------------------------------------- #
# Task 7: AdvancedStats
# --------------------------------------------------------------------------- #

def _mk_trade(pos, pnl, pips, exit_t, r=None):
    from analyzer.account_monitor import ClosedTrade
    return ClosedTrade(position_id=pos, symbol="XAUUSD", type="BUY",
                       volume=0.1, entry_time=exit_t - 100, exit_time=exit_t,
                       entry_price=4500.0, exit_price=4500.0 + pnl,
                       profit=pnl, swap=0.0, commission=0.0,
                       pips=pips, r_multiple=r)


def test_advanced_stats_streaks_and_curve():
    from analyzer.account_monitor import PerformanceEngine
    conn = MagicMock(); eng = PerformanceEngine(conn)
    trades = tuple(_mk_trade(i, p, p, 1000.0 + i * 86400, r=p / 50.0)
                   for i, p in enumerate([100, 50, -30, -20, 40]))
    a = eng._advanced_stats(trades)
    assert a.max_win_streak == 2
    assert a.max_loss_streak == 2
    assert a.current_streak == 1
    assert a.equity_curve[-1] == pytest.approx(140.0)
    assert len(a.equity_curve) == 5
    assert a.max_drawdown_abs == pytest.approx(50.0)
    assert min(a.underwater_curve) == pytest.approx(-50.0)
    assert sum(a.r_distribution.values()) == 5


def test_advanced_stats_sharpe_sortino_positive_series():
    from analyzer.account_monitor import PerformanceEngine
    conn = MagicMock(); eng = PerformanceEngine(conn)
    trades = tuple(_mk_trade(i, p, p, 1000.0 + i * 86400, r=1.0)
                   for i, p in enumerate([10, 20, 15, 25, 30]))
    a = eng._advanced_stats(trades)
    assert a.sharpe is not None and a.sharpe > 0
    assert a.sortino is None or a.sortino > 0
    assert a.var_95 is not None
    assert a.cvar_95 is not None


def test_advanced_stats_empty():
    from analyzer.account_monitor import PerformanceEngine
    conn = MagicMock(); eng = PerformanceEngine(conn)
    a = eng._advanced_stats(())
    assert a.equity_curve == []
    assert a.sharpe is None
    assert a.max_win_streak == 0


# --------------------------------------------------------------------------- #
# Task 8: EdgeStats
# --------------------------------------------------------------------------- #

def test_edge_stats_by_alignment_and_session():
    from analyzer.account_monitor import PerformanceEngine, ClosedTrade

    def tr(pos, pnl, ctx, exit_t):
        return ClosedTrade(position_id=pos, symbol="XAUUSD", type="BUY",
                           volume=0.1, entry_time=exit_t - 3600, exit_time=exit_t,
                           entry_price=4500.0, exit_price=4500.0 + pnl,
                           profit=pnl, swap=0.0, commission=0.0,
                           pips=pnl, ctx=ctx)

    up = {"M15": {"ae": True, "adx": 28.0, "rsi": 60.0}}
    dn = {"M15": {"ae": False, "adx": 18.0, "rsi": 40.0}}
    trades = (tr(1, 100, up, 1_700_000_000),
              tr(2, -50, up, 1_700_003_600),
              tr(3, 80, dn, 1_700_007_200))
    eng = PerformanceEngine(MagicMock())
    e = eng._edge_stats(trades)

    above = e.by_alignment["M15_above"]
    assert above["n"] == 2
    assert above["win_rate"] == pytest.approx(0.5)
    assert "25~30" in e.by_adx
    assert any(k for k in e.by_hold_min)


def test_edge_stats_env_axis_when_present():
    from analyzer.account_monitor import PerformanceEngine, ClosedTrade
    t = ClosedTrade(position_id=1, symbol="XAUUSD", type="BUY", volume=0.1,
                    entry_time=0.0, exit_time=3600.0, entry_price=4500.0,
                    exit_price=4600.0, profit=100.0, swap=0.0, commission=0.0,
                    pips=100.0, env={"cot_pctile": 5.0})
    e = PerformanceEngine(MagicMock())._edge_stats((t,))
    assert e.by_cot_extreme["low"]["n"] == 1


# --------------------------------------------------------------------------- #
# Task 9: compute() attaches trades/advanced/edge to PerformanceSnapshot
# --------------------------------------------------------------------------- #

def test_compute_attaches_trades_advanced_edge(monkeypatch):
    import pandas as pd
    from analyzer import account_monitor as am
    from analyzer.account_monitor import PerformanceEngine

    conn = MagicMock()
    conn.history_deals.return_value = (
        _deal(ticket=2, position_id=1, symbol="XAUUSD",
              type_=mt5.DEAL_TYPE_BUY, entry=mt5.DEAL_ENTRY_IN, volume=0.1,
              price=4500.0, profit=0.0, time_=time.time() - 3600),
        _deal(ticket=3, position_id=1, symbol="XAUUSD",
              type_=mt5.DEAL_TYPE_BUY, entry=mt5.DEAL_ENTRY_OUT, volume=0.1,
              price=4520.0, profit=200.0, time_=time.time() - 1800),
    )
    conn.account_snapshot.return_value = None
    conn.copy_rates_range.return_value = pd.DataFrame({"high": [4525.0], "low": [4498.0]})
    monkeypatch.setattr(am.journal_store, "load_recent",
                        lambda server, limit=500: [
                            {"ts": 1, "ticket": 1, "side": "BUY", "sl": 4490.0,
                             "tp": 4560.0, "ctx": {"M15": {"ae": True}}, "env": {}}])

    snap = PerformanceEngine(conn).compute()
    assert snap.trades and snap.trades[0].pips == pytest.approx(200.0)
    assert snap.trades[0].r_multiple == pytest.approx(2.0)
    assert snap.advanced is not None
    assert snap.edge is not None
    assert snap.advanced.equity_curve[-1] == pytest.approx(200.0)
