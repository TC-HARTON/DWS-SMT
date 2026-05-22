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
