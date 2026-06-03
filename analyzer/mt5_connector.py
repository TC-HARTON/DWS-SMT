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
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

import config

log = logging.getLogger(__name__)

# Server-clock offset detection guards (see :meth:`MT5Connector.server_offset_sec`).
# A LIVE tick is ~0 s old, so a true (whole-hour) broker offset shows up as a
# ``server_time - now`` that lands within seconds of a whole hour. A STALE tick
# (market closed / fresh reconnect) is arbitrarily old, so its remainder from the
# nearest whole hour is large — that is the signature we reject, because rounding
# it would invent a wrong whole-hour offset and re-stamp every bar (the live
# trigger-history duplication bug).
_OFFSET_FRESH_TOL_SEC: int = 180          # max remainder from a whole hour for a "fresh" sample
_OFFSET_MIN_SEC: int = -12 * 3600         # sane broker-offset range (whole hours)
_OFFSET_MAX_SEC: int = 14 * 3600


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
    trade_allowed: bool = True   # account AND terminal both permit live trading


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
        # Broker server-clock offset from UTC (whole seconds). MT5 stamps
        # bar/tick times in SERVER time, not UTC — see :meth:`server_offset_sec`.
        #   _server_offset_sec  : validated value cached for THIS connection
        #                         (reset on every reconnect for a possible DST flip).
        #   _last_good_offset_sec: last validated value; SURVIVES reconnects so a
        #                         reconnect that lands on a stale tick keeps using
        #                         the known-good offset instead of mis-detecting.
        #   _pending_offset_sec : a fresh value that disagrees with the known-good
        #                         one, held until it repeats (one-cycle confirmation)
        #                         so a lone hour-aligned fluke can't flip a good offset.
        self._server_offset_sec: int | None = None
        self._last_good_offset_sec: int | None = None
        self._pending_offset_sec: int | None = None
        # Actual MT5 server name from the live account (e.g. "ICMarketsSC-MT5-3").
        # Used to pick a DST-aware IANA zone for bar timestamps when the server
        # observes DST — see :data:`config.BROKER_TZ_BY_SERVER` and ``copy_rates``.
        self._connected_server: str | None = None

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
            self._connected_server = getattr(ai, "server", None) or None
            # Force re-validation of the server-clock offset for this connection
            # (a reconnect could land on a server in a different DST phase). We
            # deliberately keep ``_last_good_offset_sec`` so a reconnect onto a
            # stale tick falls back to the known-good value instead of mis-detecting.
            self._server_offset_sec = None

        # symbol resolution happens after we release the init lock so further
        # callers see a consistent state.
        self._resolve_symbols([s.base for s in config.SYMBOLS])
        # DXY dollar-context: resolve the active front-month index future and
        # register it under base "DXY". Wrapped so a DXY miss/failure NEVER
        # blocks startup — XAUUSD must still come up (DXY then degrades).
        try:
            self.resolve_dxy()
        except Exception:  # noqa: BLE001 — DXY is optional context, never fatal
            log.exception("resolve_dxy failed at startup; DXY context disabled")

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

    def resolve_dxy(self, prefix: str = config.DXY_SYMBOL_PREFIX) -> str | None:
        """Resolve the ACTIVE FRONT-MONTH dollar-index futures contract and
        register it under base 'DXY' in self._resolved, so latest_tick('DXY') /
        copy_rates('DXY', ...) work.

        The broker lists quarterly DXY_* contracts (e.g. DXY_M6 = Jun-2026,
        DXY_U6 = Sep-2026). Selection is roll-safe in two stages:
          1. keep only contracts with a LIVE (non-zero) bid — an expired
             front month stops quoting, so it drops out automatically;
          2. among the live ones pick the NEAREST contract month (front month)
             via the futures month-code in the name — both the front and back
             month tick simultaneously off the same feed, so tick recency
             cannot tell them apart; the month code can. Falls back to the
             freshest tick only when no name parses.
        Returns the chosen broker symbol, or None if no live DXY contract
        exists (the feature then degrades gracefully)."""
        import re
        import datetime as _dt
        # CME/ICE futures month codes F..Z = Jan..Dec.
        month_code = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
                      "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}
        now = _dt.datetime.now(_dt.timezone.utc)

        def _contract_key(name: str) -> tuple[int, int] | None:
            """(year, month) expiry key from a '<prefix>_<code><yeardigit>' name."""
            m = re.search(r"_([FGHJKMNQUVXZ])(\d{1,2})$", name.upper())
            if not m:
                return None
            month = month_code[m.group(1)]
            yd = m.group(2)
            if len(yd) == 1:
                year = (now.year // 10) * 10 + int(yd)
                if year < now.year - 1:        # single digit rolled into next decade
                    year += 10
            else:
                year = 2000 + int(yd)
            return (year, month)

        with self._lock:
            all_syms = mt5.symbols_get()
        if not all_syms:
            log.warning("resolve_dxy: mt5.symbols_get returned no symbols")
            return None
        key = prefix.upper()
        candidates = [s.name for s in all_syms if s.name.upper().startswith(key)]
        if not candidates:
            log.warning("resolve_dxy: no DXY_* contracts found (prefix=%r)", prefix)
            return None

        # (name, expiry_key|None, tick_time) for every LIVE-bid contract.
        live: list[tuple[str, tuple[int, int] | None, int]] = []
        with self._lock:
            for name in candidates:
                if not mt5.symbol_select(name, True):
                    code, msg = mt5.last_error()
                    log.warning("resolve_dxy: symbol_select failed for %s: code=%s msg=%r",
                                name, code, msg)
                    continue
                tick = mt5.symbol_info_tick(name)
                if tick is None:
                    continue
                bid = getattr(tick, "bid", 0.0) or 0.0
                if bid <= 0:
                    continue                              # expired / forward contract
                live.append((name, _contract_key(name),
                             int(getattr(tick, "time", 0) or 0)))
        if not live:
            log.warning("resolve_dxy: no live DXY contract (all bids 0) among %s", candidates)
            return None

        cur = (now.year, now.month)
        keyed = [x for x in live if x[1] is not None]
        upcoming = [x for x in keyed if x[1] >= cur]
        if upcoming:                       # nearest non-past contract month = front
            best = min(upcoming, key=lambda x: x[1])[0]
        elif keyed:                        # degenerate (all parsed months past): earliest
            best = min(keyed, key=lambda x: x[1])[0]
        else:                              # no parseable codes → freshest live tick
            best = max(live, key=lambda x: x[2])[0]

        with self._lock:
            self._resolved["DXY"] = best
        log.info("Resolved DXY front-month contract: %s (from live %s)",
                 best, [n for n, _, _ in live])
        return best

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

    # --------------------------------------------------------------- time
    def server_offset_sec(self) -> int:
        """Broker server-clock offset from UTC, in whole seconds (cached).

        MT5 stamps bar (``copy_rates``) and tick times in the broker's *server*
        timezone — e.g. IC Markets runs GMT+3 in summer — NOT UTC. Subtract this
        offset to recover true UTC so the live feed lines up with the UTC 16-year
        baseline and the bar-close countdown / tick age are correct.

        Derived from the FRESHEST tick's server time vs the system UTC clock,
        rounded to the nearest hour (MT5 server offsets are whole hours). The
        sample is accepted only when it is demonstrably fresh — its raw
        ``server_time - now`` sits within :data:`_OFFSET_FRESH_TOL_SEC` of a whole
        hour — and within a sane range. A stale tick (market closed / fresh
        reconnect) is rejected so a wrong whole-hour offset is never cached; when
        no fresh sample is available the last-known-good offset is returned (0 if
        none yet), so the value self-heals once live data flows. A fresh value
        that disagrees with the known-good one must repeat once before it is
        adopted (so a lone hour-aligned fluke cannot flip a good offset, while a
        real DST change still takes effect on the next cycle).

        This guards the live trigger-history store: bar timestamps are
        ``server_time - offset``, so a mis-detected offset re-stamps every bar
        and the entry-time-keyed store records the SAME trade again under a
        whole-hour-shifted timestamp.

        For a server with a known IANA zone (``config.BROKER_TZ_BY_SERVER``) the
        offset is computed DIRECTLY from that zone at the current instant — exact,
        DST-correct, and immune to stale-tick mis-detection (a tick stale by a
        whole number of hours could otherwise pass the freshness check). Bars for
        such servers are localized per-zone anyway; this keeps the tick age and
        bar-close countdown consistent with them.
        """
        tz = config.BROKER_TZ_BY_SERVER.get(self._connected_server or "")
        if tz:
            now_utc = pd.Timestamp(time.time(), unit="s", tz="UTC")
            return int(now_utc.tz_convert(tz).utcoffset().total_seconds())

        cached = self._server_offset_sec
        if cached is not None:
            return cached
        # The freshest available tick (largest server time) across resolved
        # symbols — an illiquid symbol may carry a stale tick while a liquid one
        # is current, so prefer the most recent.
        server_time = 0
        with self._lock:
            for broker in list(self._resolved.values()):
                raw = mt5.symbol_info_tick(broker)
                t = int(getattr(raw, "time", 0) or 0) if raw is not None else 0
                if t > server_time:
                    server_time = t
        if not server_time:
            return self._last_good_offset_sec or 0       # no data yet — keep prior, do not cache
        raw_delta = server_time - time.time()
        rounded = int(round(raw_delta / 3600.0) * 3600)
        if abs(raw_delta - rounded) > _OFFSET_FRESH_TOL_SEC:
            return self._last_good_offset_sec or 0       # stale tick — keep prior, do not cache
        if not (_OFFSET_MIN_SEC <= rounded <= _OFFSET_MAX_SEC):
            return self._last_good_offset_sec or 0       # implausible — ignore
        prior = self._last_good_offset_sec
        if prior is not None and rounded != prior and self._pending_offset_sec != rounded:
            # First sighting of a changed offset — hold the known-good value and
            # wait for the next cycle to confirm it is real (DST) not a fluke.
            self._pending_offset_sec = rounded
            return prior
        self._pending_offset_sec = None
        self._server_offset_sec = rounded
        self._last_good_offset_sec = rounded
        log.info("MT5 server-clock offset detected: %+d h (server→UTC)", rounded // 3600)
        return rounded

    # --------------------------------------------------------------- ticks
    def latest_tick(self, base: str) -> Tick | None:
        """Return latest tick for *base* or None if MT5 refuses the call.

        ``time_msc`` is converted from broker-server time to true UTC ms."""
        broker = self.broker_name(base)
        with self._lock:
            raw = mt5.symbol_info_tick(broker)
        if raw is None:
            return None
        off_ms = self.server_offset_sec() * 1000
        return Tick(
            symbol=broker,
            bid=raw.bid,
            ask=raw.ask,
            last=raw.last,
            time_msc=int(raw.time_msc) - off_ms,
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
        # raw["time"] is int64 seconds-since-epoch in the broker's SERVER wall
        # clock. Convert to true UTC so the index matches the 16Y Dukascopy
        # baseline and the bar-close countdown is correct.
        df.index = self._bar_index_utc(raw["time"])
        return df

    def _bar_index_utc(self, server_secs: np.ndarray) -> pd.DatetimeIndex:
        """Convert MT5 server-time bar epochs (seconds) to a true-UTC index.

        DST-observing servers (``config.BROKER_TZ_BY_SERVER``) are localized in
        their IANA zone so EACH bar gets the correct seasonal offset — a single
        flat offset would mis-stamp off-season bars in the deep-history fetch and
        the entry-time-keyed trigger store would then duplicate trades across a
        DST boundary. DST changeovers fall on a closed-market Sunday, so no bar
        lands in the ambiguous / nonexistent hour; ``ambiguous``/``nonexistent``
        are set defensively so a stray boundary bar still resolves deterministically.
        Other servers keep the flat detected-offset path (correct for a fixed
        broker offset, e.g. Exness)."""
        tz = config.BROKER_TZ_BY_SERVER.get(self._connected_server or "")
        if tz:
            naive = pd.DatetimeIndex(server_secs.astype("datetime64[s]"))
            return (naive.tz_localize(tz, ambiguous=True, nonexistent="shift_forward")
                    .tz_convert("UTC").rename("time"))
        off = self.server_offset_sec()
        times = server_secs - off if off else server_secs
        return pd.DatetimeIndex(times.astype("datetime64[s]"), tz="UTC", name="time")

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
            ti = mt5.terminal_info()
            raw_positions = mt5.positions_get() or ()

        if ai is None:
            return None
        # Live trading needs BOTH the account permission and the terminal's
        # Algo-Trading toggle. The order panel disables itself when this is False.
        trade_allowed = bool(getattr(ai, "trade_allowed", False)
                             and (ti is None or getattr(ti, "trade_allowed", False)))

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
            trade_allowed=trade_allowed,
        )

    # --------------------------------------------------------------- orders
    def _filling_mode(self, info: Any) -> int:
        """Pick a filling mode the symbol accepts (IOC > FOK > RETURN). MT5's
        ``filling_mode`` is a bitmask of SYMBOL_FILLING_FOK(1)/IOC(2).

        *info* is an ``mt5.SymbolInfo`` (the package ships no type stub)."""
        fm = int(getattr(info, "filling_mode", 0) or 0)
        if fm & 2:
            return mt5.ORDER_FILLING_IOC
        if fm & 1:
            return mt5.ORDER_FILLING_FOK
        return mt5.ORDER_FILLING_RETURN

    def _normalize_lot(self, info: Any, lots: float) -> float:
        """Clamp lots to broker volume_min/max/step AND the app cap, snapped to
        the broker's volume step at the step's OWN decimal precision (so a
        0.001- or 0.1-step broker is handled correctly, not force-rounded to
        2 dp). A hard ceiling independent of the UI input. Sub-minimum requests
        are rejected by the caller, so this never silently *inflates* a lot.

        *info* is an ``mt5.SymbolInfo`` (the package ships no type stub)."""
        vmin = float(getattr(info, "volume_min", 0.01) or 0.01)
        vmax = float(getattr(info, "volume_max", 100.0) or 100.0)
        step = float(getattr(info, "volume_step", 0.01) or 0.01)
        cap = min(vmax, float(config.ORDER_MAX_LOT))
        lot = max(vmin, min(cap, float(lots)))
        lot = vmin + round((lot - vmin) / step) * step
        lot = max(vmin, min(cap, lot))
        ndigits = 0 if step >= 1 else max(2, int(round(-math.log10(step))))
        return round(lot, ndigits)

    @staticmethod
    def _order_result(res, lot: float) -> dict:
        """Normalise an ``order_send`` result into a JSON-able dict."""
        if res is None:
            code, msg = mt5.last_error()
            return {"ok": False, "error": f"order_send returned None ({code}: {msg})"}
        ok = int(res.retcode) == int(mt5.TRADE_RETCODE_DONE)
        return {
            "ok": ok,
            "retcode": int(res.retcode),
            "comment": str(getattr(res, "comment", "") or ""),
            "order": int(getattr(res, "order", 0) or 0),
            "deal": int(getattr(res, "deal", 0) or 0),
            "price": float(getattr(res, "price", 0.0) or 0.0),
            "volume": float(getattr(res, "volume", lot) or lot),
        }

    def place_market_order(self, base: str, side: str, lots: float,
                           sl: float | None = None, tp: float | None = None) -> dict:
        """Send a market BUY/SELL for *base*. Returns a result dict; never raises
        for a trading error. Guards: app master switch, account + terminal trade
        permission, and a hard lot clamp (``ORDER_MAX_LOT`` + broker limits)."""
        if not config.TRADING_ENABLED:
            return {"ok": False, "error": "trading disabled in config (TRADING_ENABLED)"}
        if side not in ("BUY", "SELL"):
            return {"ok": False, "error": f"bad side {side!r}"}
        broker = self.broker_name(base)
        with self._lock:
            info = mt5.symbol_info(broker)
            tick = mt5.symbol_info_tick(broker)
            ai = mt5.account_info()
            ti = mt5.terminal_info()
            if info is None or tick is None:
                return {"ok": False, "error": f"no symbol/tick for {broker}"}
            if ai is None or not getattr(ai, "trade_allowed", False):
                return {"ok": False, "error": "account is not allowed to trade"}
            if ti is not None and not getattr(ti, "trade_allowed", False):
                return {"ok": False, "error": "terminal Algo Trading is disabled"}
            vmin = float(getattr(info, "volume_min", 0.01) or 0.01)
            if float(lots) < vmin:
                # Never silently bump a sub-minimum request up to vmin: that
                # would open a position LARGER than the user asked for.
                return {"ok": False,
                        "error": f"requested lot {lots} is below the broker minimum {vmin}"}
            is_buy = side == "BUY"
            lot = self._normalize_lot(info, lots)
            req = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": broker,
                "volume": lot,
                "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
                "price": float(tick.ask if is_buy else tick.bid),
                "deviation": int(config.ORDER_DEVIATION_POINTS),
                "magic": int(config.ORDER_MAGIC),
                "comment": "DWS-dash",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self._filling_mode(info),
            }
            if sl is not None:
                req["sl"] = float(sl)
            if tp is not None:
                req["tp"] = float(tp)
            res = mt5.order_send(req)
        return self._order_result(res, lot)

    def close_position(self, ticket: int) -> dict:
        """Close one open position by ticket with an opposite market deal."""
        if not config.TRADING_ENABLED:
            return {"ok": False, "error": "trading disabled in config (TRADING_ENABLED)"}
        with self._lock:
            poss = mt5.positions_get(ticket=int(ticket)) or ()
            if not poss:
                return {"ok": False, "error": f"position {ticket} not found"}
            p = poss[0]
            info = mt5.symbol_info(p.symbol)
            tick = mt5.symbol_info_tick(p.symbol)
            if info is None or tick is None:
                return {"ok": False, "error": "no symbol/tick"}
            # Same permission gate as opening (a programmatic close still goes
            # through order_send, so it needs trade permission + Algo enabled).
            # Surfaces a clean message instead of an opaque broker reject.
            ai = mt5.account_info()
            ti = mt5.terminal_info()
            if ai is None or not getattr(ai, "trade_allowed", False):
                return {"ok": False, "error": "account is not allowed to trade"}
            if ti is not None and not getattr(ti, "trade_allowed", False):
                return {"ok": False, "error": "terminal Algo Trading is disabled"}
            is_buy = p.type == mt5.POSITION_TYPE_BUY
            req = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": p.symbol,
                "volume": float(p.volume),
                "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
                "position": int(p.ticket),
                "price": float(tick.bid if is_buy else tick.ask),
                "deviation": int(config.ORDER_DEVIATION_POINTS),
                "magic": int(config.ORDER_MAGIC),
                "comment": "DWS-dash close",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self._filling_mode(info),
            }
            res = mt5.order_send(req)
        return self._order_result(res, float(p.volume))

    def close_all(self, base: str | None = None) -> dict:
        """Close every open position (optionally only *base*'s broker symbol)."""
        target = self.broker_name(base) if base else None
        with self._lock:
            poss = mt5.positions_get() or ()
        tickets = [int(p.ticket) for p in poss if target is None or p.symbol == target]
        results = [self.close_position(t) for t in tickets]   # sequential, lock not nested
        return {
            "ok": all(r.get("ok") for r in results) if results else True,
            "closed": sum(1 for r in results if r.get("ok")),
            "n": len(results),
            "results": results,
        }

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
