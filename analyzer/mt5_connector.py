"""MetaTrader 5 connection, symbol resolution, rate fetching, account snapshot.

This module is the *only* place that touches the ``MetaTrader5`` package.
Higher layers receive plain numpy/pandas objects or dataclasses so the
dashboard logic stays trivially mockable in tests.

Threading note
--------------
The ``MetaTrader5`` package serialises all calls through a single IPC
channel to the terminal. The connector is therefore intentionally
synchronous; parallelism over symbols is achieved with a
``ThreadPoolExecutor`` whose workers each acquire :attr:`MT5Connector.lock`
before issuing a call. This avoids the IPC corruption issues that arise
when concurrent threads call ``mt5.copy_rates_from_pos`` without
serialisation.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

import config

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Dataclasses returned to higher layers
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Tick:
    """Latest bid/ask/last for a single resolved symbol."""

    symbol: str          # broker-side actual symbol name
    bid: float
    ask: float
    last: float
    time_msc: int        # epoch ms


@dataclass(frozen=True)
class AccountSnapshot:
    """SPEC 14.1 リアルタイム情報。"""

    login: int
    server: str
    company: str
    currency: str
    balance: float
    equity: float
    profit: float
    margin: float
    margin_free: float
    margin_level: float      # %
    leverage: int
    positions: tuple["PositionRow", ...]


@dataclass(frozen=True)
class PositionRow:
    ticket: int
    symbol: str
    type: str           # "BUY" or "SELL"
    volume: float
    price_open: float
    price_current: float
    sl: float
    tp: float
    profit: float
    swap: float
    time: int            # epoch seconds


# --------------------------------------------------------------------------- #
# Connector
# --------------------------------------------------------------------------- #

class MT5ConnectionError(RuntimeError):
    """Raised when MT5 cannot be initialised or has dropped its connection."""


def pip_size_for(base: str, digits: int, point: float) -> float:
    """Conventional **pip size in price units** for *base*.

    Metals price a pip in DOLLARS, independent of the broker's digit precision:
      * Gold (XAU): 1 pip = $0.10 — whether the broker quotes 2 digits
        (point 0.01) or 3 digits (point 0.001). The naive even/odd-digit rule
        below yields $0.01 for BOTH, i.e. a 10x-too-small pip, so gold spreads
        render 10x too large; this override fixes that.

    FX follows the MT5 digit convention:
      * odd digits (3 or 5) — "pipette" pricing, 1 pip = 10 * point
        (EURUSD 5-digit → 0.0001, USDJPY 3-digit → 0.01)
      * even digits (2 or 4) — legacy pricing, the point IS the pip.
    """
    if base.upper().startswith("XAU"):
        return 0.10
    return point * 10.0 if digits % 2 == 1 else point


class MT5Connector:
    """Thread-safe wrapper around the MetaTrader5 package.

    The connector owns the lifetime of ``mt5.initialize()`` / ``mt5.shutdown()``
    and resolves the broker-specific symbol name for each configured base symbol
    once at startup (SPEC 7 + user instruction: "起動時に mt5.symbols_get() で
    探索してマッチング").
    """

    def __init__(
        self,
        terminal_path: str = config.MT5_TERMINAL_PATH,
        login: str = config.MT5_LOGIN,
        password: str = config.MT5_PASSWORD,
        server: str = config.MT5_SERVER,
        timeout_ms: int = config.MT5_TIMEOUT_MS,
        reconnect_interval_sec: float = config.MT5_RECONNECT_INTERVAL_SEC,
        fetch_workers: int = config.SYMBOL_FETCH_WORKERS,
    ) -> None:
        self._terminal_path = terminal_path
        self._login = login
        self._password = password
        self._server = server
        self._timeout_ms = timeout_ms
        self._reconnect_interval = reconnect_interval_sec
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=fetch_workers, thread_name_prefix="mt5-fetch"
        )
        # Resolved broker symbol names keyed by the base name from config.SYMBOLS.
        self._resolved: dict[str, str] = {}
        self._initialised = False

    # ------------------------------------------------------------------ lock
    @property
    def lock(self) -> threading.Lock:
        return self._lock

    # ------------------------------------------------------------- lifecycle
    def initialize(self) -> None:
        """Open the IPC connection to the running terminal.

        Raises:
            MT5ConnectionError: if the terminal cannot be reached or the saved
                login fails. Caller is expected to retry per
                :attr:`config.MT5_RECONNECT_INTERVAL_SEC`.
        """
        with self._lock:
            kwargs: dict[str, object] = {
                "path": self._terminal_path,
                "timeout": self._timeout_ms,
            }
            if self._login and self._password and self._server:
                try:
                    kwargs["login"] = int(self._login)
                except ValueError as exc:
                    raise MT5ConnectionError(
                        f"MT5_LOGIN must be an integer, got {self._login!r}"
                    ) from exc
                kwargs["password"] = self._password
                kwargs["server"] = self._server

            ok = mt5.initialize(**kwargs)
            if not ok:
                code, msg = mt5.last_error()
                raise MT5ConnectionError(
                    f"mt5.initialize failed: code={code} msg={msg!r} path={self._terminal_path!r}"
                )

            ti = mt5.terminal_info()
            ai = mt5.account_info()
            if ti is None or ai is None:
                mt5.shutdown()
                raise MT5ConnectionError(
                    "mt5.initialize returned True but terminal_info/account_info is None"
                )
            log.info(
                "MT5 connected: login=%s server=%s company=%s currency=%s balance=%.2f trade_allowed=%s",
                ai.login, ai.server, ai.company, ai.currency, ai.balance, ti.trade_allowed,
            )
            self._initialised = True

        # symbol resolution happens after we release the init lock so further
        # callers see a consistent state.
        self._resolve_symbols([s.base for s in config.SYMBOLS])

    def shutdown(self) -> None:
        """Close the IPC connection and tear down the worker pool."""
        with self._lock:
            if self._initialised:
                mt5.shutdown()
                self._initialised = False
        self._executor.shutdown(wait=False, cancel_futures=True)
        log.info("MT5 connector shut down")

    def ensure_connected(self) -> None:
        """Best-effort reconnect; raises on failure so callers can back off."""
        with self._lock:
            if self._initialised and mt5.terminal_info() is not None:
                return
            self._initialised = False  # forget stale state
        self.initialize()

    # ----------------------------------------------------- symbol resolution
    def _resolve_symbols(self, bases: Iterable[str]) -> None:
        """Map each base name to the broker-side actual name and select it.

        Algorithm (per user instruction): query :func:`mt5.symbols_get` once
        and find for each base the shortest broker symbol whose uppercased
        name starts with the base (handles suffixes like ``XAUUSDm``,
        ``XAUUSD.a`` while preferring exact matches).
        """
        with self._lock:
            all_syms = mt5.symbols_get()
        if not all_syms:
            raise MT5ConnectionError("mt5.symbols_get returned no symbols")

        upper_index: dict[str, list[str]] = {}
        for sym in all_syms:
            upper_index.setdefault(sym.name.upper(), []).append(sym.name)
            # also index without separators so XAUUSD.a matches "XAUUSD"
            stem = sym.name.upper().split(".")[0]
            upper_index.setdefault(stem, []).append(sym.name)

        resolved: dict[str, str] = {}
        for base in bases:
            key = base.upper()
            candidates: list[str] = []
            if key in upper_index:
                candidates = upper_index[key]
            else:
                # fallback: any symbol whose stem starts with the base
                candidates = [
                    s.name for s in all_syms
                    if s.name.upper().split(".")[0].startswith(key)
                ]
            if not candidates:
                raise MT5ConnectionError(f"Symbol not found on broker for base={base!r}")

            # Prefer exact case match, then shortest name (less suffix), then alphabetical.
            candidates_unique = sorted(set(candidates), key=lambda n: (n.upper() != key, len(n), n))
            chosen = candidates_unique[0]
            resolved[base] = chosen
            with self._lock:
                if not mt5.symbol_select(chosen, True):
                    code, msg = mt5.last_error()
                    raise MT5ConnectionError(
                        f"mt5.symbol_select failed for {chosen!r}: code={code} msg={msg!r}"
                    )

        # Publish under the lock so concurrent broker_name() readers never
        # observe a partially-populated mapping during reconnect.
        with self._lock:
            self._resolved = resolved
        log.info("Resolved %d symbols: %s", len(resolved), resolved)

    @property
    def resolved_symbols(self) -> dict[str, str]:
        """Mapping from SPEC base symbol → broker-side actual name."""
        return dict(self._resolved)

    def resolve_optional(self, bases: Iterable[str]) -> dict[str, str]:
        """Resolve broker names for *additional* symbols without failing on misses.

        Used by Phase 3 currency-strength and correlation modules that need
        crosses beyond SPEC §7's primary 10. Symbols missing on the broker
        are logged and silently omitted from the returned map. Successful
        resolutions are merged into ``self._resolved`` so subsequent
        ``broker_name()`` calls succeed.
        """
        with self._lock:
            all_syms = mt5.symbols_get()
        if not all_syms:
            return {}
        upper_index: dict[str, list[str]] = {}
        for sym in all_syms:
            upper_index.setdefault(sym.name.upper(), []).append(sym.name)
            stem = sym.name.upper().split(".")[0]
            upper_index.setdefault(stem, []).append(sym.name)

        added: dict[str, str] = {}
        for base in bases:
            # Read existing resolution under the same lock that primary
            # _resolve_symbols and reconnect paths use to publish writes;
            # without it the broadcaster could observe a half-built dict.
            with self._lock:
                existing = self._resolved.get(base)
            if existing is not None:
                added[base] = existing
                continue
            key = base.upper()
            candidates = upper_index.get(key) or [
                s.name for s in all_syms
                if s.name.upper().split(".")[0].startswith(key)
            ]
            if not candidates:
                log.info("optional symbol %s not available on broker, skipped", base)
                continue
            candidates_unique = sorted(set(candidates), key=lambda n: (n.upper() != key, len(n), n))
            chosen = candidates_unique[0]
            with self._lock:
                if not mt5.symbol_select(chosen, True):
                    code, msg = mt5.last_error()
                    log.warning("symbol_select failed for optional %s: code=%s msg=%r",
                                chosen, code, msg)
                    continue
                self._resolved[base] = chosen
            added[base] = chosen
        return added

    def broker_name(self, base: str) -> str:
        """Return the broker-side name for a configured base symbol."""
        try:
            return self._resolved[base]
        except KeyError as exc:
            raise MT5ConnectionError(
                f"Symbol {base!r} was not resolved at startup. "
                "Did you call initialize()?"
            ) from exc

    def symbol_meta_dict(self) -> dict[str, dict[str, float]]:
        """Per-symbol numeric metadata from ``mt5.symbol_info``.

        Returns ``{base: {digits, point, trade_tick_size, trade_tick_value,
        trade_contract_size, pip_value, pip_size}}``. Computed once and used
        by the frontend to render spreads/SL/TP in the broker's native pip
        units (avoids hard-coding pip multipliers per symbol).

        ``pip_size`` is computed by :func:`pip_size_for` — the MT5 digit
        convention for FX, with a metals override (gold's pip is $0.10
        regardless of whether the broker quotes 2 or 3 digits).
        """
        out: dict[str, dict[str, float]] = {}
        with self._lock:
            for base, broker in self._resolved.items():
                info = mt5.symbol_info(broker)
                if info is None:
                    continue
                digits = int(info.digits)
                point  = float(info.point)
                # pip_size follows the MT5 digit convention for FX, with a
                # metals override (gold's pip is $0.10 regardless of digits).
                pip_size = pip_size_for(base, digits, point)
                out[base] = {
                    "digits": digits,
                    "point": point,
                    "pip_size": pip_size,
                    "trade_tick_size": float(getattr(info, "trade_tick_size", point) or point),
                    "trade_tick_value": float(getattr(info, "trade_tick_value", 0.0) or 0.0),
                    "trade_contract_size": float(getattr(info, "trade_contract_size", 0.0) or 0.0),
                }
        return out

    # --------------------------------------------------------------- ticks
    def latest_tick(self, base: str) -> Tick | None:
        """Return latest tick for *base* or None if MT5 refuses the call."""
        broker = self.broker_name(base)
        with self._lock:
            raw = mt5.symbol_info_tick(broker)
        if raw is None:
            return None
        return Tick(
            symbol=broker,
            bid=raw.bid,
            ask=raw.ask,
            last=raw.last,
            time_msc=raw.time_msc,
        )

    def latest_ticks(self, bases: Iterable[str] | None = None) -> dict[str, Tick]:
        """Fetch ticks for many symbols in parallel.

        Failures (e.g. transient ``None`` from MT5) are silently skipped — the
        caller can decide whether the absence of a symbol is an error.
        """
        targets = list(bases) if bases is not None else list(self._resolved.keys())
        out: dict[str, Tick] = {}
        if not targets:
            return out
        futures = {self._executor.submit(self.latest_tick, b): b for b in targets}
        for fut in as_completed(futures):
            base = futures[fut]
            try:
                tick = fut.result()
            except Exception:  # noqa: BLE001 — log and continue per SPEC 18.4
                log.exception("latest_tick failed for %s", base)
                continue
            if tick is not None:
                out[base] = tick
        return out

    # --------------------------------------------------------------- rates
    def copy_rates(self, base: str, mt5_timeframe: int, count: int) -> pd.DataFrame:
        """Return ``count`` most recent OHLC bars as a DataFrame indexed by UTC time.

        Columns: ``open``, ``high``, ``low``, ``close``, ``tick_volume``,
        ``spread``, ``real_volume``. Empty DataFrame if MT5 returned None.

        Performance
        -----------
        Building DataFrame straight from the MT5 structured-numpy array via
        ``pd.DataFrame(raw)`` and then ``pd.to_datetime`` was the dominant
        cost of a full analysis cycle (~3 ms / call × 70 calls = ~200 ms).
        We instead hand pandas a dict of pre-typed numpy views and convert
        the epoch column directly via ``np.datetime64[s]`` — same final
        shape, roughly 4× faster on the same input.
        """
        broker = self.broker_name(base)
        with self._lock:
            raw = mt5.copy_rates_from_pos(broker, mt5_timeframe, 0, count)
        if raw is None or len(raw) == 0:
            log.warning("copy_rates_from_pos returned empty for %s tf=%s", broker, mt5_timeframe)
            return pd.DataFrame()
        # Pull only the fields we actually consume; iterating the dtype keeps
        # us forward-compatible if MetaTrader5 adds new columns and graceful
        # if it ever renames one (the missing field is simply dropped instead
        # of raising at construction time).
        wanted = ("open", "high", "low", "close",
                  "tick_volume", "spread", "real_volume")
        available = [c for c in wanted if c in raw.dtype.names]
        df = pd.DataFrame({col: raw[col] for col in available})
        # raw["time"] is int64 seconds-since-epoch — DatetimeIndex directly
        # from numpy datetime64 skips pandas' string-aware parsing path.
        df.index = pd.DatetimeIndex(
            raw["time"].astype("datetime64[s]"), tz="UTC", name="time",
        )
        return df

    def fetch_rates_parallel(
        self,
        bases: Iterable[str],
        timeframes: Iterable[config.TimeframeSpec],
    ) -> dict[tuple[str, str], pd.DataFrame]:
        """Fetch rates for every (base, tf) pair in parallel.

        Returns:
            Dict keyed by ``(base, tf.label)``. Missing pairs (MT5 failure) are
            omitted, never raised.
        """
        targets = [(b, tf) for b in bases for tf in timeframes]
        out: dict[tuple[str, str], pd.DataFrame] = {}
        if not targets:
            return out
        futures = {
            self._executor.submit(self.copy_rates, b, tf.mt5_const, tf.bars_to_fetch): (b, tf)
            for b, tf in targets
        }
        for fut in as_completed(futures):
            base, tf = futures[fut]
            try:
                df = fut.result()
            except Exception:  # noqa: BLE001 — log and continue
                log.exception("copy_rates failed for %s/%s", base, tf.label)
                continue
            if not df.empty:
                out[(base, tf.label)] = df
        return out

    # -------------------------------------------------------------- account
    def account_snapshot(self) -> AccountSnapshot | None:
        """Compose a SPEC-14.1 snapshot of account state + open positions.

        Returns ``None`` if MT5 returned no account info — caller should mark
        the connection as down and trigger a reconnect.
        """
        with self._lock:
            ai = mt5.account_info()
            raw_positions = mt5.positions_get() or ()

        if ai is None:
            return None

        positions = tuple(
            PositionRow(
                ticket=p.ticket,
                symbol=p.symbol,
                type="BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
                volume=p.volume,
                price_open=p.price_open,
                price_current=p.price_current,
                sl=p.sl,
                tp=p.tp,
                profit=p.profit,
                swap=p.swap,
                time=p.time,
            )
            for p in raw_positions
        )
        return AccountSnapshot(
            login=ai.login,
            server=ai.server,
            company=ai.company,
            currency=ai.currency,
            balance=ai.balance,
            equity=ai.equity,
            profit=ai.profit,
            margin=ai.margin,
            margin_free=ai.margin_free,
            margin_level=ai.margin_level,
            leverage=ai.leverage,
            positions=positions,
        )

    # -------------------------------------------------------- trade history
    def history_deals(self, from_ts: float, to_ts: float | None = None) -> tuple:
        """Wrapper around :func:`mt5.history_deals_get`.

        Args:
            from_ts: epoch seconds (UTC) lower bound, inclusive.
            to_ts: epoch seconds (UTC) upper bound, exclusive. ``None`` → now.

        Returns the raw tuple of :class:`mt5.TradeDeal` records. Empty tuple
        if MT5 returns None (no deals or query failed).
        """
        if to_ts is None:
            to_ts = time.time()
        from_dt = datetime.fromtimestamp(from_ts, tz=timezone.utc)
        to_dt = datetime.fromtimestamp(to_ts, tz=timezone.utc)
        with self._lock:
            raw = mt5.history_deals_get(from_dt, to_dt)
        return raw or ()

    # ------------------------------------------------------- context manager
    def __enter__(self) -> "MT5Connector":
        self.initialize()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown()
