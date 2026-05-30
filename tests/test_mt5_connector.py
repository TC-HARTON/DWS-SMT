"""MT5 connector tests with the MetaTrader5 module fully mocked.

The connector is intentionally the only module that touches
``MetaTrader5``; mocking ``analyzer.mt5_connector.mt5`` exercises the
real connector code paths without any IPC dependency.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from analyzer.mt5_connector import (
    MT5ConnectionError,
    MT5Connector,
    PositionRow,
)


# --------------------------------------------------------------- helpers

class _FakeSymbol:
    """Minimal stand-in for the ``SymbolInfo`` named tuple MT5 returns."""
    def __init__(self, name: str) -> None:
        self.name = name


def _make_terminal_info() -> SimpleNamespace:
    return SimpleNamespace(
        path=r"C:\Program Files\MetaTrader 5 EXNESS",
        connected=True,
        trade_allowed=False,
    )


def _make_account_info() -> SimpleNamespace:
    return SimpleNamespace(
        login=12345, server="Broker-Demo", company="Broker Ltd",
        currency="USD", balance=10_000.0, equity=10_050.0, profit=50.0,
        margin=200.0, margin_free=9_850.0, margin_level=5025.0, leverage=500,
    )


# --------------------------------------------------------------- fixtures

@pytest.fixture
def mt5_stub(mocker):
    """Replace ``analyzer.mt5_connector.mt5`` with a manipulable MagicMock."""
    fake = MagicMock(name="mt5_module")
    fake.POSITION_TYPE_BUY = 0
    fake.TIMEFRAME_D1 = 16408
    fake.TIMEFRAME_H4 = 16388
    fake.TIMEFRAME_H1 = 16385
    fake.TIMEFRAME_M15 = 15
    fake.initialize.return_value = True
    fake.last_error.return_value = (1, "Success")
    fake.terminal_info.return_value = _make_terminal_info()
    fake.account_info.return_value = _make_account_info()
    fake.symbols_get.return_value = [
        _FakeSymbol("XAUUSD"), _FakeSymbol("USDJPY"), _FakeSymbol("EURUSD"),
        _FakeSymbol("GBPUSD"), _FakeSymbol("AUDUSD"), _FakeSymbol("GBPJPY"),
        _FakeSymbol("EURJPY"), _FakeSymbol("AUDJPY"),
    ]
    fake.symbol_select.return_value = True
    fake.positions_get.return_value = ()
    mocker.patch("analyzer.mt5_connector.mt5", fake)
    return fake


# --------------------------------------------------------------- tests

def test_initialize_raises_when_mt5_returns_false(mt5_stub):
    mt5_stub.initialize.return_value = False
    mt5_stub.last_error.return_value = (-6, "Authorization failed")
    c = MT5Connector(terminal_path="X", login="", password="", server="")
    with pytest.raises(MT5ConnectionError, match="Authorization failed"):
        c.initialize()


def test_initialize_resolves_symbols_for_exact_match(mt5_stub):
    c = MT5Connector(terminal_path="X", login="", password="", server="")
    c.initialize()
    resolved = c.resolved_symbols
    assert resolved["XAUUSD"] == "XAUUSD"
    assert resolved["EURUSD"] == "EURUSD"
    # symbol_select was called once per resolved symbol
    assert mt5_stub.symbol_select.call_count == 8


def test_initialize_resolves_symbols_with_broker_suffix(mt5_stub):
    mt5_stub.symbols_get.return_value = [
        _FakeSymbol("XAUUSDm"),
        _FakeSymbol("USDJPYm"),
        _FakeSymbol("EURUSDm"),
        _FakeSymbol("GBPUSDm"),
        _FakeSymbol("AUDUSDm"),
        _FakeSymbol("GBPJPYm"),
        _FakeSymbol("EURJPYm"),
        _FakeSymbol("AUDJPYm"),
    ]
    c = MT5Connector(terminal_path="X", login="", password="", server="")
    c.initialize()
    assert c.broker_name("XAUUSD") == "XAUUSDm"
    assert c.broker_name("USDJPY") == "USDJPYm"


def test_initialize_prefers_exact_over_suffixed(mt5_stub):
    mt5_stub.symbols_get.return_value = [
        _FakeSymbol("XAUUSDm"),  # suffix variant first
        _FakeSymbol("XAUUSD"),   # exact match
        _FakeSymbol("USDJPY"), _FakeSymbol("EURUSD"), _FakeSymbol("GBPUSD"),
        _FakeSymbol("AUDUSD"), _FakeSymbol("GBPJPY"), _FakeSymbol("EURJPY"),
        _FakeSymbol("AUDJPY"),
    ]
    c = MT5Connector(terminal_path="X", login="", password="", server="")
    c.initialize()
    assert c.broker_name("XAUUSD") == "XAUUSD"


def test_initialize_raises_when_symbol_missing(mt5_stub):
    mt5_stub.symbols_get.return_value = [_FakeSymbol("USDJPY")]  # only one
    c = MT5Connector(terminal_path="X", login="", password="", server="")
    with pytest.raises(MT5ConnectionError, match="Symbol not found"):
        c.initialize()


def test_latest_tick_returns_none_when_mt5_returns_none(mt5_stub):
    c = MT5Connector(terminal_path="X", login="", password="", server="")
    c.initialize()
    mt5_stub.symbol_info_tick.return_value = None
    assert c.latest_tick("XAUUSD") is None


def test_latest_tick_returns_dataclass(mt5_stub):
    c = MT5Connector(terminal_path="X", login="", password="", server="")
    c.initialize()
    mt5_stub.symbol_info_tick.return_value = SimpleNamespace(
        bid=4500.0, ask=4500.5, last=4500.25, time_msc=1_700_000_000_000,
    )
    t = c.latest_tick("XAUUSD")
    assert t is not None
    assert t.bid == 4500.0
    assert t.ask == 4500.5
    assert t.symbol == "XAUUSD"


def test_server_offset_converts_times_to_utc(mt5_stub, mocker):
    """MT5 stamps bar/tick times in SERVER time; the connector must subtract the
    server→UTC offset. Simulate a broker 3h ahead of UTC (e.g. IC EEST)."""
    c = MT5Connector(terminal_path="X", login="", password="", server="")
    c.initialize()
    fake_now = 1_700_000_000.0
    mocker.patch("analyzer.mt5_connector.time.time", return_value=fake_now)
    server_t = int(fake_now) + 3 * 3600          # broker clock is UTC+3
    mt5_stub.symbol_info_tick.return_value = SimpleNamespace(
        bid=1.0, ask=2.0, last=1.5, time=server_t, time_msc=server_t * 1000 + 250,
    )
    # Offset detected and rounded to whole hours.
    assert c.server_offset_sec() == 3 * 3600
    # Tick time_msc converted back to true UTC ms.
    t = c.latest_tick("XAUUSD")
    assert t.time_msc == int(fake_now) * 1000 + 250
    # Bar times shifted by -3h so the index is true UTC.
    raw = np.array(
        [(server_t, 100.0, 101.0, 99.0, 100.5, 10, 0, 0)],
        dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"),
               ("close", "f8"), ("tick_volume", "i8"), ("spread", "i4"),
               ("real_volume", "i8")],
    )
    mt5_stub.copy_rates_from_pos.return_value = raw
    df = c.copy_rates("XAUUSD", mt5_timeframe=15, count=1)
    assert df.index[-1].value // 1_000_000_000 == int(fake_now)   # epoch secs (UTC)


def test_server_offset_rejects_stale_tick(mt5_stub, mocker):
    """A STALE tick (market closed / just reconnected) is hours old, so its
    ``server_time - now`` sits far from any whole hour. Rounding it would invent
    a wrong whole-hour offset that re-stamps every bar (the trigger-dup bug), so
    the connector must REJECT such a sample (no cache) and self-heal once a fresh
    tick flows."""
    c = MT5Connector(terminal_path="X", login="", password="", server="")
    c.initialize()
    fake_now = 1_700_000_000.0
    mocker.patch("analyzer.mt5_connector.time.time", return_value=fake_now)
    # Tick is ~3h+20min "ahead": the 20-min remainder marks it as NOT a fresh,
    # whole-hour-aligned offset sample (a live tick would be within seconds).
    stale_t = int(fake_now) + 3 * 3600 + 1200
    mt5_stub.symbol_info_tick.return_value = SimpleNamespace(
        bid=1.0, ask=2.0, last=1.5, time=stale_t, time_msc=stale_t * 1000,
    )
    assert c.server_offset_sec() == 0          # rejected, no prior -> 0
    # Not cached: a subsequent FRESH tick self-heals to the true offset.
    fresh_t = int(fake_now) + 3 * 3600
    mt5_stub.symbol_info_tick.return_value = SimpleNamespace(
        bid=1.0, ask=2.0, last=1.5, time=fresh_t, time_msc=fresh_t * 1000,
    )
    assert c.server_offset_sec() == 3 * 3600


def test_server_offset_keeps_last_good_across_reconnect_when_stale(mt5_stub, mocker):
    """Once a good offset is known it must survive a reconnect that lands on a
    stale tick — returning the last-known-good value rather than 0 or a corrupt
    re-detection."""
    c = MT5Connector(terminal_path="X", login="", password="", server="")
    c.initialize()
    fake_now = 1_700_000_000.0
    mocker.patch("analyzer.mt5_connector.time.time", return_value=fake_now)
    fresh_t = int(fake_now) + 3 * 3600
    mt5_stub.symbol_info_tick.return_value = SimpleNamespace(
        bid=1.0, ask=2.0, last=1.5, time=fresh_t, time_msc=fresh_t * 1000,
    )
    assert c.server_offset_sec() == 3 * 3600          # learn the good offset

    c.initialize()                                     # reconnect resets per-conn cache
    stale_t = int(fake_now) + 3 * 3600 + 1500          # 25-min remainder -> stale
    mt5_stub.symbol_info_tick.return_value = SimpleNamespace(
        bid=1.0, ask=2.0, last=1.5, time=stale_t, time_msc=stale_t * 1000,
    )
    assert c.server_offset_sec() == 3 * 3600          # last-good, not corrupted


def test_server_offset_requires_confirmation_to_change(mt5_stub, mocker):
    """A lone fresh sample that DIFFERS from the known-good offset must not flip
    it on the spot (guards an hour-aligned fluke); a genuine change (e.g. DST)
    repeats and is adopted on the next cycle."""
    c = MT5Connector(terminal_path="X", login="", password="", server="")
    c.initialize()
    fake_now = 1_700_000_000.0
    mocker.patch("analyzer.mt5_connector.time.time", return_value=fake_now)
    good_t = int(fake_now) + 3 * 3600
    mt5_stub.symbol_info_tick.return_value = SimpleNamespace(
        bid=1.0, ask=2.0, last=1.5, time=good_t, time_msc=good_t * 1000,
    )
    assert c.server_offset_sec() == 3 * 3600          # known-good = +3h

    c._server_offset_sec = None                        # force re-detection
    dst_t = int(fake_now) + 2 * 3600                    # offset now +2h (fresh, aligned)
    mt5_stub.symbol_info_tick.return_value = SimpleNamespace(
        bid=1.0, ask=2.0, last=1.5, time=dst_t, time_msc=dst_t * 1000,
    )
    assert c.server_offset_sec() == 3 * 3600          # first sighting -> hold prior
    c._server_offset_sec = None
    assert c.server_offset_sec() == 2 * 3600          # confirmed -> adopt


def test_server_offset_for_dst_broker_is_tz_derived_not_tick(mt5_stub, mocker):
    """For a DST-aware server the offset comes from the IANA zone at the current
    instant — exact and immune to stale ticks (a tick stale by whole hours could
    pass the freshness check). 2023-11-14 is EET (+2h) for Europe/Bucharest."""
    mt5_stub.account_info.return_value = SimpleNamespace(
        login=1, server="ICMarketsSC-MT5-3", company="IC", currency="JPY",
        balance=0.0, equity=0.0, profit=0.0, margin=0.0, margin_free=0.0,
        margin_level=0.0, leverage=500,
    )
    c = MT5Connector(terminal_path="X", login="", password="", server="")
    c.initialize()
    mocker.patch("analyzer.mt5_connector.time.time", return_value=1_700_000_000.0)
    # A wildly stale tick must be IGNORED for a DST broker (tz decides).
    mt5_stub.symbol_info_tick.return_value = SimpleNamespace(
        bid=1.0, ask=2.0, last=1.5, time=1_700_000_000 - 6 * 3600, time_msc=0,
    )
    assert c.server_offset_sec() == 2 * 3600          # EET in November, from the zone


def test_copy_rates_dst_aware_server_uses_iana_zone(mt5_stub):
    """A DST-observing server (config.BROKER_TZ_BY_SERVER) must stamp each bar
    with the correct SEASONAL offset — +2h in winter (EET), +3h in summer (EEST)
    for Europe/Bucharest — instead of one flat offset that would mis-stamp
    off-season bars and duplicate trades across a DST boundary."""
    mt5_stub.account_info.return_value = SimpleNamespace(
        login=1, server="ICMarketsSC-MT5-3", company="IC", currency="JPY",
        balance=0.0, equity=0.0, profit=0.0, margin=0.0, margin_free=0.0,
        margin_level=0.0, leverage=500,
    )
    c = MT5Connector(terminal_path="X", login="", password="", server="")
    c.initialize()

    winter = int(pd.Timestamp("2026-01-15 12:00:00").timestamp())   # server wall clock
    summer = int(pd.Timestamp("2026-07-15 12:00:00").timestamp())
    raw = np.array(
        [(winter, 1.0, 1.0, 1.0, 1.0, 1, 0, 0),
         (summer, 2.0, 2.0, 2.0, 2.0, 1, 0, 0)],
        dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"),
               ("close", "f8"), ("tick_volume", "i8"), ("spread", "i4"),
               ("real_volume", "i8")],
    )
    mt5_stub.copy_rates_from_pos.return_value = raw
    df = c.copy_rates("XAUUSD", mt5_timeframe=15, count=2)
    # Winter server 12:00 EET(+2) -> 10:00 UTC; summer 12:00 EEST(+3) -> 09:00 UTC.
    assert df.index[0] == pd.Timestamp("2026-01-15 10:00:00", tz="UTC")
    assert df.index[1] == pd.Timestamp("2026-07-15 09:00:00", tz="UTC")


def test_account_snapshot_includes_open_positions(mt5_stub):
    c = MT5Connector(terminal_path="X", login="", password="", server="")
    c.initialize()
    mt5_stub.positions_get.return_value = (
        SimpleNamespace(
            ticket=1, symbol="XAUUSD", type=0,  # POSITION_TYPE_BUY
            volume=0.10, price_open=4500.0, price_current=4520.0,
            sl=4480.0, tp=4560.0, profit=200.0, swap=-0.5, time=1_700_000_000,
        ),
    )
    snap = c.account_snapshot()
    assert snap is not None
    assert snap.login == 12345
    assert snap.balance == 10_000.0
    assert len(snap.positions) == 1
    assert isinstance(snap.positions[0], PositionRow)
    assert snap.positions[0].type == "BUY"


def test_account_snapshot_returns_none_when_account_unavailable(mt5_stub):
    c = MT5Connector(terminal_path="X", login="", password="", server="")
    c.initialize()
    mt5_stub.account_info.return_value = None
    assert c.account_snapshot() is None


def test_copy_rates_returns_dataframe_indexed_by_utc_time(mt5_stub):
    c = MT5Connector(terminal_path="X", login="", password="", server="")
    c.initialize()
    raw = np.array(
        [
            (1_700_000_000, 100.0, 101.0, 99.0, 100.5, 10, 0, 0),
            (1_700_003_600, 100.5, 102.0, 100.0, 101.5, 12, 0, 0),
        ],
        dtype=[
            ("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"),
            ("close", "f8"), ("tick_volume", "i8"), ("spread", "i4"),
            ("real_volume", "i8"),
        ],
    )
    mt5_stub.copy_rates_from_pos.return_value = raw
    df = c.copy_rates("XAUUSD", mt5_timeframe=15, count=2)
    assert not df.empty
    assert df.index.tz is not None
    assert list(df.columns) >= ["open", "high", "low", "close"]
    assert df["close"].iloc[-1] == pytest.approx(101.5)


def test_copy_rates_returns_empty_df_when_mt5_returns_none(mt5_stub):
    c = MT5Connector(terminal_path="X", login="", password="", server="")
    c.initialize()
    mt5_stub.copy_rates_from_pos.return_value = None
    df = c.copy_rates("XAUUSD", mt5_timeframe=15, count=10)
    assert df.empty
