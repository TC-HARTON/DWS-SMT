"""EMA-stack oscillator: compute correctness, repaint-free property, wire shape.

Exercised with a stub connector (no MetaTrader5) so the derivation is verified
in isolation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from analyzer.ema_stack import EmaStackSnapshot, _ema, compute_ema_stack
import config


class _StubConnector:
    """Minimal stand-in exposing only what compute_ema_stack reads."""

    def __init__(self, resolved: dict[str, str], rates_df: pd.DataFrame | None) -> None:
        self._resolved = resolved
        self._rates_df = rates_df if rates_df is not None else pd.DataFrame()

    @property
    def resolved_symbols(self) -> dict[str, str]:
        return dict(self._resolved)

    def copy_rates(self, base: str, tf: int, count: int) -> pd.DataFrame:
        return self._rates_df


def _ramp_df(n: int, *, start: float = 2000.0, step: float = 0.2) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    close = start + step * np.arange(n, dtype=float)
    return pd.DataFrame({"open": close, "high": close + 1,
                         "low": close - 1, "close": close}, index=idx)


def test_unresolved_returns_stale():
    snap = compute_ema_stack(_StubConnector({"NOTGOLD": "X"}, _ramp_df(500)))
    assert isinstance(snap, EmaStackSnapshot)
    assert snap.symbol is None and snap.stale is True
    assert snap.dev_price == () and snap.price is None


def test_too_little_history_returns_stale():
    # < center period (320) confirmed bars → stale (center EMA not meaningful).
    snap = compute_ema_stack(_StubConnector({"XAUUSD": "XAUUSD"}, _ramp_df(100)))
    assert snap.symbol == "XAUUSD" and snap.stale is True


def test_populated_snapshot_shape_and_center():
    df = _ramp_df(1500)
    snap = compute_ema_stack(_StubConnector({"XAUUSD": "XAUUSD"}, df))
    assert snap.stale is False and snap.symbol == "XAUUSD"
    assert snap.periods == (20, 80, 320)
    n = config.EMA_STACK_DISPLAY_BARS
    assert len(snap.times_ms) == len(snap.dev_price) == len(snap.dev_fast) == len(snap.dev_mid) == n
    # On a rising ramp the fast EMAs sit ABOVE the center EMA → positive deviation.
    assert snap.dev_fast[-1] > 0 and snap.dev_mid[-1] > 0
    # The forming (last) bar was dropped: emitted price is the prior close.
    assert snap.price == df["close"].to_numpy()[-2]


def test_confirmed_bars_never_repaint_when_new_bar_arrives():
    """Adding a new (forming) bar must not change any already-confirmed bar's
    EMA deviations — EMAs are causal and only confirmed bars are emitted, so
    there is no repaint. (No multi-TF mapping exists to introduce look-ahead.)"""
    df = _ramp_df(1500)
    a = compute_ema_stack(_StubConnector({"XAUUSD": "XAUUSD"}, df))
    # Append one more bar (a new forming candle) and recompute.
    extra = _ramp_df(1501)
    b = compute_ema_stack(_StubConnector({"XAUUSD": "XAUUSD"}, extra))
    # Compare bars present (by time) in BOTH emitted windows: identical.
    common = set(a.times_ms) & set(b.times_ms)
    assert len(common) > 50            # the windows overlap substantially
    ai = {t: i for i, t in enumerate(a.times_ms)}
    bi = {t: i for i, t in enumerate(b.times_ms)}
    for t in common:
        assert a.dev_price[ai[t]] == b.dev_price[bi[t]], f"repaint @ {t} (price)"
        assert a.dev_fast[ai[t]] == b.dev_fast[bi[t]], f"repaint @ {t} (fast)"
        assert a.dev_mid[ai[t]] == b.dev_mid[bi[t]], f"repaint @ {t} (mid)"


def test_serialize_ema_stack_shape():
    from dashboard.serialize import serialize_ema_stack

    assert serialize_ema_stack(None) is None
    snap = compute_ema_stack(_StubConnector({"XAUUSD": "XAUUSD"}, _ramp_df(1500)))
    out = serialize_ema_stack(snap)
    for key in ("symbol", "periods", "price", "ema_fast", "ema_mid",
                "ema_center", "t", "dev_price", "dev_fast", "dev_mid", "stale"):
        assert key in out
    assert out["periods"] == [20, 80, 320]
    assert len(out["t"]) == len(out["dev_price"])


def test_ema_matches_pandas_adjust_false():
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    got = _ema(x, 3)
    want = pd.Series(x).ewm(span=3, adjust=False).mean().to_numpy()
    np.testing.assert_allclose(got, want, rtol=1e-12)
