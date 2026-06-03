"""DXY feed: compute_dxy snapshot logic + serialize_dxy wire shape.

``compute_dxy`` is exercised with lightweight stub connectors (no MetaTrader5
dependency) so the snapshot derivation is verified in isolation.
"""

from __future__ import annotations

import pandas as pd
import pytest

from analyzer.dxy_feed import DxySnapshot, compute_dxy


class _StubConnector:
    """Minimal stand-in for MT5Connector exposing only what compute_dxy reads.

    ``resolved`` maps base → broker symbol (as the real ``resolved_symbols``
    property returns); ``rates_df`` is returned verbatim from ``copy_rates``.
    """

    def __init__(self, resolved: dict[str, str], rates_df: pd.DataFrame | None = None) -> None:
        self._resolved = resolved
        self._rates_df = rates_df if rates_df is not None else pd.DataFrame()

    @property
    def resolved_symbols(self) -> dict[str, str]:
        return dict(self._resolved)

    def copy_rates(self, base: str, mt5_timeframe: int, count: int) -> pd.DataFrame:
        return self._rates_df


def _make_dxy_df(closes: list[float]) -> pd.DataFrame:
    """Build an OHLC DataFrame mimicking the columns copy_rates returns."""
    n = len(closes)
    return pd.DataFrame(
        {
            "open": [c - 0.05 for c in closes],
            "high": [c + 0.10 for c in closes],
            "low": [c - 0.10 for c in closes],
            "close": closes,
            "tick_volume": [100] * n,
            "spread": [2] * n,
            "real_volume": [0] * n,
        },
        index=pd.date_range("2026-06-01", periods=n, freq="h", tz="UTC"),
    )


# ----------------------------------------------------------- compute_dxy

def test_compute_dxy_unresolved_returns_stale():
    conn = _StubConnector(resolved={"XAUUSD": "XAUUSD"})  # no DXY
    snap = compute_dxy(conn)
    assert isinstance(snap, DxySnapshot)
    assert snap.symbol is None
    assert snap.stale is True
    assert snap.closes == ()
    assert snap.price is None
    assert snap.change is None
    assert snap.ema is None


def test_compute_dxy_empty_rates_returns_stale_with_symbol():
    conn = _StubConnector(resolved={"DXY": "DXY_M6"}, rates_df=pd.DataFrame())
    snap = compute_dxy(conn)
    assert snap.symbol == "DXY_M6"
    assert snap.stale is True
    assert snap.closes == ()
    assert snap.price is None


def test_compute_dxy_populated_snapshot():
    # 5 bars; the LAST is the still-forming bar that compute_dxy must drop.
    # Closed closes are therefore [99.0, 99.5, 99.2, 99.8].
    closes = [99.0, 99.5, 99.2, 99.8, 999.0]
    conn = _StubConnector(resolved={"DXY": "DXY_M6"}, rates_df=_make_dxy_df(closes))
    snap = compute_dxy(conn)

    assert snap.symbol == "DXY_M6"
    assert snap.stale is False
    # Forming bar dropped → price is the last CLOSED close, prev is the prior one.
    assert snap.price == 99.8
    assert snap.prev_close == 99.2
    assert snap.change == pytest.approx(99.8 - 99.2)
    assert snap.change_pct == pytest.approx((99.8 - 99.2) / 99.2 * 100.0)
    assert isinstance(snap.ema, float)
    assert snap.above_ema in (True, False)
    assert len(snap.closes) > 0
    # Sparkline closes are the closed-bar closes (forming bar excluded).
    assert snap.closes[-1] == 99.8
    assert 999.0 not in snap.closes


# ----------------------------------------------------------- serialize_dxy

def test_serialize_dxy_none():
    from dashboard.serialize import serialize_dxy
    assert serialize_dxy(None) is None


def test_serialize_dxy_populated_shape():
    import time
    from dashboard.serialize import serialize_dxy

    snap = DxySnapshot(
        symbol="DXY_M6", price=99.8, prev_close=99.2,
        change=0.6, change_pct=0.605, ema=99.4, above_ema=True,
        closes=(99.0, 99.5, 99.2, 99.8), as_of=time.time(), stale=False,
    )
    out = serialize_dxy(snap)
    for key in ("symbol", "price", "change", "change_pct", "ema",
                "above_ema", "closes", "stale"):
        assert key in out
    assert out["symbol"] == "DXY_M6"
    assert out["price"] == pytest.approx(99.8)
    assert out["above_ema"] is True
    assert out["closes"] == [99.0, 99.5, 99.2, 99.8]
    assert out["stale"] is False
