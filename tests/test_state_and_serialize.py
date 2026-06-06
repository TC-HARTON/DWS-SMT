"""LatestState concurrency primitive + JSON serialiser shape tests."""

from __future__ import annotations

import threading
import time

import pandas as pd
import pytest

from analyzer.indicator_engine import (
    AnalysisSnapshot,
    SymbolIndicators,
    TimeframeIndicators,
)
from analyzer.mt5_connector import AccountSnapshot, PositionRow, Tick
from analyzer.state import (
    ConnectionStatus,
    LatestState,
    PriceSnapshot,
)
from dashboard.serialize import snapshot_to_json


# ---------------------------------------------------- LatestState behaviour

def test_writes_bump_version():
    s = LatestState()
    assert s.version == 0
    s.set_status(ConnectionStatus(True, None, time.time()))
    assert s.version == 1
    s.set_price(PriceSnapshot(generated_at=time.time(), ticks={}))
    assert s.version == 2


def test_wait_for_update_returns_true_on_new_version():
    s = LatestState()
    s.set_status(ConnectionStatus(True, None, time.time()))
    v0 = s.version

    def writer():
        time.sleep(0.05)
        s.set_status(ConnectionStatus(False, "err", time.time()))

    threading.Thread(target=writer).start()
    t0 = time.perf_counter()
    got = s.wait_for_update(v0, timeout=2.0)
    elapsed = time.perf_counter() - t0
    assert got is True
    assert elapsed < 1.0  # woke before timeout
    assert s.version > v0


def test_wait_for_update_returns_false_on_timeout():
    s = LatestState()
    v0 = s.version
    got = s.wait_for_update(v0, timeout=0.05)
    assert got is False


# ----------------------------------------------------------- serialiser

def _fake_account() -> AccountSnapshot:
    return AccountSnapshot(
        login=1, server="S", company="C", currency="USD",
        balance=10.0, equity=11.0, profit=1.0, margin=2.0,
        margin_free=8.0, margin_level=500.0, leverage=100,
        positions=(
            PositionRow(
                ticket=1, symbol="XAUUSD", type="BUY", volume=0.1,
                price_open=4500.0, price_current=4510.0,
                sl=0.0, tp=0.0, profit=100.0, swap=0.0, time=1,
            ),
        ),
    )


def _fake_analysis() -> AnalysisSnapshot:
    bar_time = pd.Timestamp("2026-05-18T12:00:00", tz="UTC")
    tf = TimeframeIndicators(
        label="H1", last_close=1.25, ema=1.20, ema_period=20, above_ema=True,
        rsi=55.0, atr=0.001, adx=27.0, di_plus=18.0, di_minus=12.0,
        bar_time=bar_time,
    )
    sym = SymbolIndicators(base="EURUSD", broker_name="EURUSD", by_tf={"H1": tf})
    return AnalysisSnapshot(
        generated_at=pd.Timestamp.now("UTC"),
        compute_ms=12.3,
        by_symbol={"EURUSD": sym},
    )


def test_snapshot_to_json_has_expected_keys():
    s = LatestState()
    s.set_status(ConnectionStatus(True, None, time.time()))
    s.set_price(PriceSnapshot(
        generated_at=time.time(),
        ticks={"XAUUSD": Tick("XAUUSD", 4500.0, 4500.5, 4500.25, 1_700_000_000_000)},
    ))
    s.set_account(_fake_account())
    s.set_analysis(_fake_analysis())

    blob = snapshot_to_json(s)
    assert blob["version"] >= 4
    assert blob["status"]["connected"] is True
    assert blob["price"]["ticks"]["XAUUSD"]["bid"] == pytest.approx(4500.0)
    assert blob["account"]["balance"] == pytest.approx(10.0)
    assert len(blob["account"]["positions"]) == 1
    assert blob["account"]["positions"][0]["type"] == "BUY"
    tf = blob["analysis"]["by_symbol"]["EURUSD"]["by_tf"]["H1"]
    assert tf["ema"] == pytest.approx(1.20)
    assert tf["above_ema"] is True
    assert "symbol_order" in blob and "XAUUSD" in blob["symbol_order"]


def test_snapshot_to_json_handles_empty_state():
    blob = snapshot_to_json(LatestState())
    assert blob["status"]["connected"] is False
    assert blob["price"] is None
    assert blob["analysis"] is None
    assert blob["account"] is None
    assert blob["version"] == 0


def test_serialize_coerces_nan_to_none():
    s = LatestState()
    bar_time = pd.Timestamp("2026-05-18T12:00:00", tz="UTC")
    tf = TimeframeIndicators(
        label="H1", last_close=1.0,
        ema=float("nan"), ema_period=20, above_ema=None,
        rsi=None, atr=None, adx=None, di_plus=None, di_minus=None,
        bar_time=bar_time,
    )
    sym = SymbolIndicators(base="EURUSD", broker_name="EURUSD", by_tf={"H1": tf})
    s.set_analysis(AnalysisSnapshot(
        generated_at=pd.Timestamp.now("UTC"), compute_ms=1.0,
        by_symbol={"EURUSD": sym},
    ))
    blob = snapshot_to_json(s)
    assert blob["analysis"]["by_symbol"]["EURUSD"]["by_tf"]["H1"]["ema"] is None


def test_state_set_and_read_macro():
    from analyzer.state import LatestState
    from analyzer.macro_feed import MacroSnapshot

    st = LatestState()
    assert st.macro is None
    before = st.analysis_version
    snap = MacroSnapshot(generated_at=1.0, fetched_at=1.0, rates={},
                         employment=None, by_pair={}, last_error=None,
                         consecutive_failures=0)
    st.set_macro(snap)
    assert st.macro is snap
    assert st.analysis_version == before + 1


def test_serialize_real_yield_shape():
    from dashboard.serialize import serialize_real_yield
    from analyzer.macro_feed import RealYieldSnapshot

    snap = RealYieldSnapshot(value=1.92, prev_value=1.88, change_1d=0.04,
                             trend_5d=0.15, gold_dir=-1, as_of="2026-05-21",
                             stale=False, generated_at=1.0)
    out = serialize_real_yield(snap)
    assert out["value"] == 1.92
    assert out["gold_dir"] == -1
    assert out["change_1d"] == 0.04
    assert out["stale"] is False
    assert serialize_real_yield(None) is None


def test_state_set_and_read_real_yield():
    from analyzer.state import LatestState
    from analyzer.macro_feed import RealYieldSnapshot

    st = LatestState()
    assert st.real_yield is None
    before = st.analysis_version
    snap = RealYieldSnapshot(value=1.9, prev_value=1.85, change_1d=0.05,
                             trend_5d=0.12, gold_dir=-1, as_of="2026-05-21",
                             stale=False, generated_at=1.0)
    st.set_real_yield(snap)
    assert st.real_yield is snap
    assert st.analysis_version == before + 1


def test_serialize_macro_shape():
    from dashboard.serialize import serialize_macro
    from analyzer.macro_feed import MacroRate, MacroPairBias, MacroSnapshot

    rates = {"USD": MacroRate("USD", 4.5, "2026-05-01", 4.25, "fred", False)}
    pair = MacroPairBias("USDJPY", "USD", "JPY", 4.0, 1, "USD金利優位")
    snap = MacroSnapshot(generated_at=1.0, fetched_at=1.0, rates=rates,
                         employment=None, by_pair={"USDJPY": pair},
                         last_error=None, consecutive_failures=0)
    out = serialize_macro(snap)
    assert out["rates"]["USD"]["rate"] == 4.5
    assert out["rates"]["USD"]["stale"] is False
    assert out["by_pair"]["USDJPY"]["macro_dir"] == 1
    assert out["by_pair"]["USDJPY"]["differential"] == 4.0
    assert serialize_macro(None) is None


# --------------------------------------------------------------------------- #
# Task 10: serialize_performance includes trades/advanced/edge
# --------------------------------------------------------------------------- #

def test_serialize_performance_includes_trades_advanced_edge():
    from analyzer.account_monitor import (
        PerformanceSnapshot, ClosedTrade, AdvancedStats, EdgeStats)
    from dashboard.serialize import serialize_performance

    t = ClosedTrade(position_id=1, symbol="XAUUSD", type="BUY", volume=0.1,
                    entry_time=100.0, exit_time=200.0, entry_price=4500.0,
                    exit_price=4520.0, profit=200.0, swap=0.0, commission=0.0,
                    pips=200.0, mae_pips=-20.0, mfe_pips=250.0, r_multiple=2.0,
                    sl=4490.0, tp=4560.0, ctx={"M15": {"ae": True}}, env={})
    adv = AdvancedStats(sharpe=1.5, sortino=2.0, calmar=3.0, recovery_factor=3.0,
                        ulcer_index=1.1, var_95=-50.0, cvar_95=-80.0,
                        max_win_streak=3, max_loss_streak=1, current_streak=2,
                        max_drawdown_abs=50.0, underwater_pct=0.2,
                        r_distribution={"1~2": 3}, equity_curve=[200.0],
                        underwater_curve=[0.0])
    edge = EdgeStats(by_alignment={"M15_above": {"n": 2, "win_rate": 0.5, "pf": 1.2}},
                     by_adx={}, by_rsi={}, by_weekday_jst={}, by_hold_min={},
                     by_dxy={}, by_cot_extreme={}, by_real_yield={}, by_flip={})
    snap = PerformanceSnapshot(
        generated_at=1.0, compute_ms=1.0, fetched_from_ts=0.0, fetched_to_ts=2.0,
        by_range={}, open_trade_count=0, today_realised_pnl=0.0,
        today_floating_pnl=0.0, today_total_pnl=0.0,
        trades=(t,), advanced=adv, edge=edge)

    out = serialize_performance(snap)
    assert out["trades"][0]["pips"] == 200.0
    assert out["trades"][0]["r_multiple"] == 2.0
    assert out["trades"][0]["ctx"] == {"M15": {"ae": True}}
    assert out["advanced"]["sharpe"] == 1.5
    assert out["advanced"]["equity_curve"] == [200.0]
    assert out["edge"]["by_alignment"]["M15_above"]["win_rate"] == 0.5


def test_state_holds_both_ema_modes():
    from analyzer.state import LatestState
    from analyzer.ema_stack import EmaStackSnapshot

    def _snap(mode, periods):
        return EmaStackSnapshot(
            symbol="XAUUSD", periods=periods, price=1.0, ema_fast=1.0,
            ema_mid=1.0, ema_center=1.0, times_ms=(1,), dev_price=(0.0,),
            dev_fast=(0.0,), dev_mid=(0.0,), as_of=1.0, stale=False, mode=mode)

    st = LatestState()
    st.set_ema_stack(_snap("M15", (20, 80, 320)))
    st.set_ema_stack(_snap("H1", (20, 80, 480)))
    blob = st.snapshot()
    assert blob["ema_stack"].mode == "M15"
    assert blob["ema_stack_h1"].mode == "H1"


def test_serialize_ema_stack_carries_mode():
    from analyzer.ema_stack import EmaStackSnapshot
    from dashboard.serialize import serialize_ema_stack
    snap = EmaStackSnapshot(
        symbol="XAUUSD", periods=(20, 80, 480), price=1.0, ema_fast=1.0,
        ema_mid=1.0, ema_center=1.0, times_ms=(1,), dev_price=(0.0,),
        dev_fast=(0.0,), dev_mid=(0.0,), as_of=1.0, stale=False, mode="H1")
    assert serialize_ema_stack(snap)["mode"] == "H1"
