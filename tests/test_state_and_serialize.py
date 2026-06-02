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


def _make_validation_snap(pfs_by_cell):
    """Build a tiny ValidationSnapshot with the given (sym, tf) → PF mapping."""
    from analyzer.signal_validator import (
        RegimeStats, SubPeriodStats, ValidationCore, ValidationStats,
        ValidationSnapshot,
    )
    third = SubPeriodStats(win_rate=0.5, expectancy=1.0, n_trades=10)
    regime = RegimeStats(win_rate=0.5, expectancy=1.0, n_trades=10)
    by_sym = {}
    for (sym, tf), pf in pfs_by_cell.items():
        core = ValidationCore(
            n_trades=100, win_rate=0.5, ci_low=0.4, ci_high=0.6,
            profit_factor=pf, expectancy=1.0, max_drawdown=10.0, avg_mae=5.0,
            thirds=(third, third, third), regime_trend=regime, regime_range=regime,
            tier="信頼",
        )
        stats = ValidationStats(symbol=sym, base_tf=tf, raw=core, macro_filtered=core)
        by_sym.setdefault(sym, {})[tf] = stats
    return ValidationSnapshot(generated_at=1.0, compute_ms=1.0, by_symbol=by_sym)


def test_validation_history_appends_per_cell():
    from analyzer.state import LatestState
    st = LatestState()
    st.set_validation(_make_validation_snap({("EURUSD", "M15"): 2.10}))
    st.set_validation(_make_validation_snap({("EURUSD", "M15"): 2.15}))
    st.set_validation(_make_validation_snap({("EURUSD", "M15"): 2.20}))
    hist = st.validation_history
    assert hist["EURUSD"]["M15"] == [2.10, 2.15, 2.20]


def test_validation_history_trims_to_cap():
    from analyzer.state import LatestState
    st = LatestState()
    # Cap is 24; push 30 entries and confirm only the last 24 survive.
    for i in range(30):
        st.set_validation(_make_validation_snap({("EURUSD", "M15"): float(i)}))
    hist = st.validation_history
    assert len(hist["EURUSD"]["M15"]) == 24
    assert hist["EURUSD"]["M15"][0] == 6.0    # entries 0..5 trimmed
    assert hist["EURUSD"]["M15"][-1] == 29.0


def test_validation_history_inf_pf_stored_as_none():
    import math
    from analyzer.state import LatestState
    st = LatestState()
    st.set_validation(_make_validation_snap({("EURUSD", "M15"): math.inf}))
    hist = st.validation_history
    assert hist["EURUSD"]["M15"] == [None]


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


def test_state_set_and_read_gold_macro():
    from analyzer.state import LatestState
    from analyzer.gold_macro import GoldMacroSnapshot
    st = LatestState()
    assert st.gold_macro is None
    snap = GoldMacroSnapshot(score=2.5, band="中立", contributions=(),
                             n_drivers=4, window=252, as_of="2026-06-01",
                             stale=False, generated_at=1.0)
    st.set_gold_macro(snap)
    assert st.gold_macro is snap
    # It must ride the FULL snapshot, never the light one.
    assert st.snapshot()["gold_macro"] is snap
    assert "gold_macro" not in st.light_snapshot()


def test_serialize_gold_macro_shape():
    from dashboard.serialize import serialize_gold_macro
    from analyzer.gold_macro import GoldMacroSnapshot, GoldDriverContribution
    snap = GoldMacroSnapshot(
        score=2.5, band="中立",
        contributions=(GoldDriverContribution(
            key="vix", label_ja="リスク(VIX)", value=18.0, z=0.5,
            signed_z=0.5, sign_gold=1),),
        n_drivers=4, window=252, as_of="2026-06-01", stale=False,
        generated_at=1.0)
    out = serialize_gold_macro(snap)
    assert out["score"] == 2.5
    assert out["band"] == "中立"
    assert out["n_drivers"] == 4
    assert out["contributions"][0]["key"] == "vix"
    assert out["contributions"][0]["sign"] == 1
    assert serialize_gold_macro(None) is None


def test_serialize_gold_macro_none_score():
    from dashboard.serialize import serialize_gold_macro
    from analyzer.gold_macro import GoldMacroSnapshot
    snap = GoldMacroSnapshot(score=None, band="データ待ち", contributions=(),
                             n_drivers=0, window=252, as_of="", stale=True,
                             generated_at=1.0)
    out = serialize_gold_macro(snap)
    assert out["score"] is None
    assert out["band"] == "データ待ち"
