"""DXY (US Dollar Index) feed — dollar context for the XAUUSD dashboard.

Gold trades inverse to the dollar, so the US Dollar Index gives a one-glance
read on the dollar backdrop. The broker exposes quarterly DXY_* index futures
(the active front month is auto-resolved under base ``"DXY"`` by
:meth:`analyzer.mt5_connector.MT5Connector.resolve_dxy`), so once it is
registered this module just pulls bars via ``connector.copy_rates("DXY", ...)``.

Display-only context — never feeds trigger / trade / order logic. The
:func:`compute_dxy` helper is defensive: it returns a stale snapshot rather
than raising into the analysis loop when DXY is unresolved or has no data.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

import config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DxySnapshot:
    """One DXY refresh — price, trend, and a sparkline of recent closes."""

    symbol: str | None            # active broker contract (e.g. "DXY_M6"), None if unresolved
    price: float | None           # latest close/price
    prev_close: float | None      # previous bar close
    change: float | None          # price - prev_close
    change_pct: float | None      # change / prev_close * 100
    ema: float | None             # EMA(DXY_EMA_PERIOD) of closes
    above_ema: bool | None        # price > ema
    closes: tuple[float, ...]     # recent closes for the sparkline (chronological)
    as_of: float                  # epoch seconds
    stale: bool                   # True when no live data


# How many trailing closes to ship for the sparkline (kept short so the WS
# payload stays small; the full window still drives price/prev/EMA).
_SPARKLINE_CLOSES: int = 60


def _stale_snapshot(symbol: str | None) -> DxySnapshot:
    """A fully-empty snapshot (no live data), optionally tagged with the symbol."""
    return DxySnapshot(
        symbol=symbol,
        price=None,
        prev_close=None,
        change=None,
        change_pct=None,
        ema=None,
        above_ema=None,
        closes=(),
        as_of=time.time(),
        stale=True,
    )


def _dxy_timeframe() -> int:
    """Map ``config.DXY_CHART_TF`` (a label) to an mt5 timeframe constant.

    Uses ``config.TIMEFRAME_BY_LABEL`` when the label is present there; falls
    back to ``mt5.TIMEFRAME_H1`` otherwise (the configured default is "H1")."""
    spec = config.TIMEFRAME_BY_LABEL.get(config.DXY_CHART_TF)
    if spec is not None:
        return spec.mt5_const
    return mt5.TIMEFRAME_H1


def compute_dxy(connector) -> DxySnapshot:
    """Compute a :class:`DxySnapshot` from the active DXY contract's bars.

    Returns a STALE empty snapshot when DXY is unresolved (``"DXY"`` absent from
    ``connector.resolved_symbols``) or the broker returns no bars. Otherwise
    fetches ``config.DXY_CHART_BARS`` bars, drops the forming last bar (closed
    bars only, like the rest of the codebase), and derives price / prev_close /
    change / change_pct / EMA / sparkline. Never raises into the analysis loop —
    all paths are guarded.
    """
    resolved = connector.resolved_symbols
    symbol = resolved.get("DXY")
    if symbol is None:
        return _stale_snapshot(None)

    try:
        df = connector.copy_rates("DXY", _dxy_timeframe(), config.DXY_CHART_BARS)
    except Exception:  # noqa: BLE001 — feed must never raise into the loop
        log.exception("compute_dxy: copy_rates failed for DXY")
        return _stale_snapshot(symbol)

    if df is None or df.empty or "close" not in df.columns:
        return _stale_snapshot(symbol)

    # Closed bars only — drop the still-forming last bar (codebase convention).
    closed = df.iloc[:-1]
    closes_arr = closed["close"].to_numpy(dtype=float)
    closes_arr = closes_arr[np.isfinite(closes_arr)]
    if closes_arr.size == 0:
        return _stale_snapshot(symbol)

    price = float(closes_arr[-1])
    prev_close = float(closes_arr[-2]) if closes_arr.size >= 2 else None

    change: float | None = None
    change_pct: float | None = None
    if prev_close is not None:
        change = price - prev_close
        if prev_close != 0:
            change_pct = change / prev_close * 100.0

    # EMA via pandas ewm (SMA-seeded would need a helper; the trailing ewm value
    # is sufficient for a display-only trend read). Guard against an all-empty
    # series — closes_arr is already non-empty here.
    ema_series = pd.Series(closes_arr).ewm(
        span=config.DXY_EMA_PERIOD, adjust=False
    ).mean()
    ema_val = float(ema_series.iloc[-1])
    ema: float | None = ema_val if np.isfinite(ema_val) else None
    above_ema: bool | None = (price > ema) if ema is not None else None

    closes = tuple(float(v) for v in closes_arr[-_SPARKLINE_CLOSES:])

    return DxySnapshot(
        symbol=symbol,
        price=price,
        prev_close=prev_close,
        change=change,
        change_pct=change_pct,
        ema=ema,
        above_ema=above_ema,
        closes=closes,
        as_of=time.time(),
        stale=False,
    )
