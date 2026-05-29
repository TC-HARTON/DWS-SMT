"""Discretionary order panel — connector order methods + server endpoints.

MetaTrader5 is fully mocked: these exercise the REAL connector/endpoint code
paths (lot clamp, trade-permission guards, filling-mode pick, opposite-deal
close, same-origin guard) without any IPC or a live account. No real orders.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import config
from analyzer.mt5_connector import MT5Connector
from dashboard.lite_server import build_app


_SYMS = ["XAUUSD", "USDJPY", "EURUSD", "GBPUSD", "AUDUSD", "GBPJPY", "EURJPY", "AUDJPY"]


@pytest.fixture
def mt5(mocker):
    fake = MagicMock(name="mt5")
    # constants
    fake.TRADE_ACTION_DEAL = 1
    fake.ORDER_TYPE_BUY = 0
    fake.ORDER_TYPE_SELL = 1
    fake.POSITION_TYPE_BUY = 0
    fake.ORDER_TIME_GTC = 0
    fake.ORDER_FILLING_FOK = 1
    fake.ORDER_FILLING_IOC = 2
    fake.ORDER_FILLING_RETURN = 3
    fake.TRADE_RETCODE_DONE = 10009
    fake.TIMEFRAME_D1 = 16408
    fake.TIMEFRAME_H4 = 16388
    fake.TIMEFRAME_H1 = 16385
    fake.TIMEFRAME_M15 = 15
    # connection
    fake.initialize.return_value = True
    fake.last_error.return_value = (1, "ok")
    fake.terminal_info.return_value = SimpleNamespace(trade_allowed=True, connected=True)
    fake.account_info.return_value = SimpleNamespace(
        login=1, server="Exness-Demo", company="X", currency="JPY",
        balance=1e6, equity=1e6, profit=0.0, margin=0.0, margin_free=1e6,
        margin_level=0.0, leverage=1000, trade_allowed=True,
    )
    fake.symbols_get.return_value = [SimpleNamespace(name=s) for s in _SYMS]
    fake.symbol_select.return_value = True
    fake.symbol_info.return_value = SimpleNamespace(
        digits=2, point=0.01, filling_mode=2,            # IOC allowed
        volume_min=0.01, volume_max=50.0, volume_step=0.01,
        trade_tick_size=0.01, trade_tick_value=1.0, trade_contract_size=100.0,
    )
    fake.symbol_info_tick.return_value = SimpleNamespace(
        bid=4500.0, ask=4500.5, last=4500.0, time=1_700_000_000, time_msc=1_700_000_000_000,
    )
    fake.order_send.return_value = SimpleNamespace(
        retcode=10009, order=111, deal=222, price=4500.5, volume=0.01, comment="done")
    fake.positions_get.return_value = ()
    mocker.patch("analyzer.mt5_connector.mt5", fake)
    return fake


@pytest.fixture
def conn(mt5):
    c = MT5Connector(terminal_path="X", login="", password="", server="")
    c.initialize()
    return c


# --------------------------------------------------------------- place order
def test_place_buy_uses_ask_and_clamps_and_tags(conn, mt5):
    res = conn.place_market_order("XAUUSD", "BUY", 999, sl=4400.0, tp=4700.0)
    assert res["ok"] is True and res["retcode"] == 10009 and res["order"] == 111
    req = mt5.order_send.call_args[0][0]
    assert req["type"] == mt5.ORDER_TYPE_BUY
    assert req["price"] == 4500.5                       # ask for BUY
    assert req["volume"] == config.ORDER_MAX_LOT         # 999 clamped to app cap (min(volume_max,ORDER_MAX_LOT))
    assert req["magic"] == config.ORDER_MAGIC
    assert req["type_filling"] == mt5.ORDER_FILLING_IOC
    assert req["sl"] == 4400.0 and req["tp"] == 4700.0


def test_place_sell_uses_bid(conn, mt5):
    res = conn.place_market_order("XAUUSD", "SELL", 0.05)
    req = mt5.order_send.call_args[0][0]
    assert req["type"] == mt5.ORDER_TYPE_SELL
    assert req["price"] == 4500.0                        # bid for SELL
    assert req["volume"] == 0.05
    assert "sl" not in req and "tp" not in req           # omitted when None


def test_lot_capped_at_order_max_lot(conn, mt5, monkeypatch):
    # volume_max huge so the app ORDER_MAX_LOT cap is the binding one.
    mt5.symbol_info.return_value = SimpleNamespace(
        digits=2, point=0.01, filling_mode=2,
        volume_min=0.01, volume_max=1000.0, volume_step=0.01)
    res = conn.place_market_order("XAUUSD", "BUY", 999)
    assert res["ok"]
    assert mt5.order_send.call_args[0][0]["volume"] == config.ORDER_MAX_LOT


def test_blocked_when_account_not_trade_allowed(conn, mt5):
    mt5.account_info.return_value = SimpleNamespace(trade_allowed=False, login=1)
    res = conn.place_market_order("XAUUSD", "BUY", 0.01)
    assert res["ok"] is False and "trade" in res["error"]
    mt5.order_send.assert_not_called()


def test_blocked_when_terminal_algo_disabled(conn, mt5):
    mt5.terminal_info.return_value = SimpleNamespace(trade_allowed=False)
    res = conn.place_market_order("XAUUSD", "BUY", 0.01)
    assert res["ok"] is False
    mt5.order_send.assert_not_called()


def test_blocked_when_trading_disabled_in_config(conn, mt5, monkeypatch):
    monkeypatch.setattr(config, "TRADING_ENABLED", False)
    res = conn.place_market_order("XAUUSD", "BUY", 0.01)
    assert res["ok"] is False
    mt5.order_send.assert_not_called()


def test_filling_mode_fallback_fok_then_return(conn, mt5):
    mt5.symbol_info.return_value = SimpleNamespace(
        digits=2, point=0.01, filling_mode=1,            # only FOK
        volume_min=0.01, volume_max=50.0, volume_step=0.01)
    conn.place_market_order("XAUUSD", "BUY", 0.01)
    assert mt5.order_send.call_args[0][0]["type_filling"] == mt5.ORDER_FILLING_FOK
    mt5.symbol_info.return_value = SimpleNamespace(
        digits=2, point=0.01, filling_mode=0,            # neither
        volume_min=0.01, volume_max=50.0, volume_step=0.01)
    conn.place_market_order("XAUUSD", "BUY", 0.01)
    assert mt5.order_send.call_args[0][0]["type_filling"] == mt5.ORDER_FILLING_RETURN


def test_order_send_none_is_handled(conn, mt5):
    mt5.order_send.return_value = None
    res = conn.place_market_order("XAUUSD", "BUY", 0.01)
    assert res["ok"] is False and "None" in res["error"]


# --------------------------------------------------------------- close
def test_close_position_opposite_deal(conn, mt5):
    mt5.positions_get.return_value = (SimpleNamespace(
        ticket=555, symbol="XAUUSD", type=mt5.POSITION_TYPE_BUY, volume=0.30),)
    res = conn.close_position(555)
    assert res["ok"]
    req = mt5.order_send.call_args[0][0]
    assert req["type"] == mt5.ORDER_TYPE_SELL              # opposite of BUY
    assert req["position"] == 555 and req["volume"] == 0.30
    assert req["price"] == 4500.0                          # bid to close a BUY


def test_close_position_not_found(conn, mt5):
    mt5.positions_get.return_value = ()
    res = conn.close_position(999)
    assert res["ok"] is False and "not found" in res["error"]
    mt5.order_send.assert_not_called()


def test_close_all_closes_each(conn, mt5):
    mt5.positions_get.return_value = (
        SimpleNamespace(ticket=1, symbol="XAUUSD", type=0, volume=0.1),
        SimpleNamespace(ticket=2, symbol="XAUUSD", type=1, volume=0.2),
    )
    res = conn.close_all()
    assert res["n"] == 2 and res["closed"] == 2 and res["ok"]


# --------------------------------------------------------------- endpoints
@pytest.fixture
def client(conn):
    app = build_app(conn)
    app.config.update(TESTING=True)
    return app.test_client()


def test_endpoint_order_ok(client, mt5):
    r = client.post("/api/order", json={"symbol": "XAUUSD", "side": "BUY", "lots": 0.02})
    assert r.status_code == 200 and r.get_json()["ok"] is True


def test_endpoint_order_unknown_symbol(client):
    r = client.post("/api/order", json={"symbol": "NOPE", "side": "BUY", "lots": 0.02})
    assert r.status_code == 400


def test_endpoint_order_bad_side(client):
    r = client.post("/api/order", json={"symbol": "XAUUSD", "side": "HOLD", "lots": 0.02})
    assert r.status_code == 400


def test_endpoint_order_cross_origin_blocked(client):
    r = client.post("/api/order", json={"symbol": "XAUUSD", "side": "BUY", "lots": 0.02},
                    headers={"Origin": "http://evil.com"})
    assert r.status_code == 403


def test_endpoint_close_all(client, mt5):
    mt5.positions_get.return_value = (
        SimpleNamespace(ticket=1, symbol="XAUUSD", type=0, volume=0.1),)
    r = client.post("/api/close", json={"all": True})
    assert r.status_code == 200 and r.get_json()["closed"] == 1


# ------------------------------------------------- money-safety edge cases
def test_lot_below_minimum_is_rejected_not_bumped(conn, mt5):
    # broker min 0.01; a 0.005 request must be REJECTED, never silently
    # inflated to 0.01 (which would open a 2x-larger position than asked).
    res = conn.place_market_order("XAUUSD", "BUY", 0.005)
    assert res["ok"] is False and "minimum" in res["error"]
    mt5.order_send.assert_not_called()


def test_lot_snaps_to_non_standard_step(conn, mt5):
    # 0.1-step broker: 0.34 snaps to 0.3 (not force-rounded to 2dp garbage).
    mt5.symbol_info.return_value = SimpleNamespace(
        digits=2, point=0.01, filling_mode=2,
        volume_min=0.1, volume_max=50.0, volume_step=0.1)
    conn.place_market_order("XAUUSD", "BUY", 0.34)
    vol = mt5.order_send.call_args[0][0]["volume"]
    assert abs(vol - 0.3) < 1e-9


def test_close_blocked_when_account_not_trade_allowed(conn, mt5):
    mt5.positions_get.return_value = (SimpleNamespace(
        ticket=555, symbol="XAUUSD", type=mt5.POSITION_TYPE_BUY, volume=0.10),)
    mt5.account_info.return_value = SimpleNamespace(trade_allowed=False, login=1)
    res = conn.close_position(555)
    assert res["ok"] is False and "trade" in res["error"]
    mt5.order_send.assert_not_called()


def test_close_blocked_when_terminal_algo_disabled(conn, mt5):
    mt5.positions_get.return_value = (SimpleNamespace(
        ticket=556, symbol="XAUUSD", type=mt5.POSITION_TYPE_BUY, volume=0.10),)
    mt5.terminal_info.return_value = SimpleNamespace(trade_allowed=False)
    res = conn.close_position(556)
    assert res["ok"] is False
    mt5.order_send.assert_not_called()


def test_endpoint_order_zero_lots(client):
    r = client.post("/api/order", json={"symbol": "XAUUSD", "side": "BUY", "lots": 0})
    assert r.status_code == 400


def test_endpoint_order_negative_lots(client):
    r = client.post("/api/order", json={"symbol": "XAUUSD", "side": "BUY", "lots": -0.5})
    assert r.status_code == 400


def test_endpoint_order_missing_lots(client):
    r = client.post("/api/order", json={"symbol": "XAUUSD", "side": "BUY"})
    assert r.status_code == 400
