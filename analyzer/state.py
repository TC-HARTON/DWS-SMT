"""Thread-safe shared cache between the background analysis loop and the dashboard.

The analysis loop is the sole writer. Dash callbacks and the WebSocket
broadcaster are readers. Both sides go through :class:`LatestState` so we
avoid the deadlock-prone pattern of holding ``MT5Connector.lock`` for the
entire dashboard render path.

Each domain (price ticks, indicators, account) is a separate snapshot that
can be updated independently — this is what lets us refresh prices at 1 s
while keeping the heavier indicator pass on a 5 s cadence.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from analyzer.account_monitor import PerformanceSnapshot
from analyzer.calendar_feed import CalendarSnapshot
from analyzer.confluence import ConfluenceCluster
from analyzer.correlation import CorrelationSnapshot
from analyzer.currency_strength import StrengthSnapshot
from analyzer.indicator_engine import AnalysisSnapshot
from analyzer.mt5_connector import AccountSnapshot, Tick
from analyzer.price_action import PriceActionEvent
from analyzer.structure_types import StructureLevel


@dataclass(frozen=True)
class PriceSnapshot:
    """One round of bid/ask ticks across every resolved symbol."""

    generated_at: float                  # epoch seconds (UTC)
    ticks: dict[str, Tick]               # keyed by base name


@dataclass(frozen=True)
class ConnectionStatus:
    connected: bool
    last_error: str | None
    last_connect_ts: float | None        # epoch seconds


@dataclass(frozen=True)
class SymbolStructures:
    """Per-symbol Phase 2 bundle: structure levels + PA events + confluences."""

    levels: tuple[StructureLevel, ...]
    price_action: tuple[PriceActionEvent, ...]
    confluences: tuple[ConfluenceCluster, ...]


@dataclass(frozen=True)
class StructuresSnapshot:
    """Per-cycle Phase 2 output keyed by base symbol."""

    generated_at: float                          # epoch seconds (UTC)
    by_symbol: dict[str, SymbolStructures]


class LatestState:
    """Read/write shared snapshots with a fine-grained RLock.

    The reader path is intended to be called from Dash callbacks /
    WebSocket broadcaster which run on multiple threads; the writer path
    is called only from :class:`analyzer.analysis_loop.AnalysisLoop`.

    Notification primitive
    ----------------------
    Writers bump :attr:`version` and call :meth:`_notify`; readers can
    block in :meth:`wait_for_update` until either a new version arrives
    or *timeout* elapses. This drives the WebSocket broadcaster without
    polling so the SPEC §19 "WS 100 ms" latency budget is comfortable.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._price: Optional[PriceSnapshot] = None
        self._analysis: Optional[AnalysisSnapshot] = None
        self._account: Optional[AccountSnapshot] = None
        self._structures: Optional[StructuresSnapshot] = None
        self._strength: Optional[StrengthSnapshot] = None
        self._correlation: Optional[CorrelationSnapshot] = None
        self._performance: Optional[PerformanceSnapshot] = None
        self._calendar: Optional[CalendarSnapshot] = None
        self._status: ConnectionStatus = ConnectionStatus(False, None, None)
        self._broker_meta: dict[str, dict[str, float]] = {}
        self._monotonic_version = 0  # bumped on any write — useful for clients
        # Bumped only by the heavy domains (analysis / structures / strength /
        # correlation / performance / calendar). Lets the WS broadcaster send
        # a light price-only message when only ticks changed, instead of
        # re-shipping the whole ~169 KB snapshot at the 2 Hz price cadence.
        self._analysis_version = 0

    def set_broker_meta(self, meta: dict[str, dict[str, float]]) -> None:
        """Broker-side per-symbol numeric metadata (digits, point, tick value).
        Populated once at startup from ``mt5.symbol_info`` — does NOT bump
        the version (it's static and shouldn't trigger paint)."""
        with self._lock:
            self._broker_meta = dict(meta)

    @property
    def broker_meta(self) -> dict[str, dict[str, float]]:
        with self._lock:
            return dict(self._broker_meta)

    # ----------------------------------------------------------- writers
    def set_price(self, snapshot: PriceSnapshot) -> None:
        with self._cond:
            self._price = snapshot
            self._monotonic_version += 1
            self._cond.notify_all()

    def set_analysis(self, snapshot: AnalysisSnapshot) -> None:
        with self._cond:
            self._analysis = snapshot
            self._monotonic_version += 1
            self._analysis_version += 1
            self._cond.notify_all()

    def set_account(self, snapshot: AccountSnapshot | None) -> None:
        with self._cond:
            self._account = snapshot
            self._monotonic_version += 1
            self._cond.notify_all()

    def set_status(self, status: ConnectionStatus) -> None:
        with self._cond:
            self._status = status
            self._monotonic_version += 1
            self._cond.notify_all()

    def set_structures(self, snapshot: StructuresSnapshot) -> None:
        with self._cond:
            self._structures = snapshot
            self._monotonic_version += 1
            self._analysis_version += 1
            self._cond.notify_all()

    def set_strength(self, snapshot: StrengthSnapshot) -> None:
        with self._cond:
            self._strength = snapshot
            self._monotonic_version += 1
            self._analysis_version += 1
            self._cond.notify_all()

    def set_correlation(self, snapshot: CorrelationSnapshot) -> None:
        with self._cond:
            self._correlation = snapshot
            self._monotonic_version += 1
            self._analysis_version += 1
            self._cond.notify_all()

    def set_performance(self, snapshot: PerformanceSnapshot) -> None:
        with self._cond:
            self._performance = snapshot
            self._monotonic_version += 1
            self._analysis_version += 1
            self._cond.notify_all()

    def set_calendar(self, snapshot: CalendarSnapshot) -> None:
        with self._cond:
            self._calendar = snapshot
            self._monotonic_version += 1
            self._analysis_version += 1
            self._cond.notify_all()

    def wait_for_update(self, since_version: int, timeout: float) -> bool:
        """Block until ``self.version > since_version`` or *timeout* elapses.

        Returns True if a new version is available, False on timeout.
        """
        with self._cond:
            return self._cond.wait_for(
                lambda: self._monotonic_version > since_version,
                timeout=timeout,
            )

    # ----------------------------------------------------------- readers
    @property
    def price(self) -> PriceSnapshot | None:
        with self._lock:
            return self._price

    @property
    def analysis(self) -> AnalysisSnapshot | None:
        with self._lock:
            return self._analysis

    @property
    def account(self) -> AccountSnapshot | None:
        with self._lock:
            return self._account

    @property
    def status(self) -> ConnectionStatus:
        with self._lock:
            return self._status

    @property
    def structures(self) -> StructuresSnapshot | None:
        with self._lock:
            return self._structures

    @property
    def strength(self) -> StrengthSnapshot | None:
        with self._lock:
            return self._strength

    @property
    def correlation(self) -> CorrelationSnapshot | None:
        with self._lock:
            return self._correlation

    @property
    def performance(self) -> PerformanceSnapshot | None:
        with self._lock:
            return self._performance

    @property
    def calendar(self) -> CalendarSnapshot | None:
        with self._lock:
            return self._calendar

    @property
    def version(self) -> int:
        with self._lock:
            return self._monotonic_version

    @property
    def analysis_version(self) -> int:
        """Counter for the heavy domains only — see :attr:`_analysis_version`."""
        with self._lock:
            return self._analysis_version

    def snapshot(self) -> dict[str, object]:
        """Return a single dict view of everything (for WS broadcast)."""
        with self._lock:
            return {
                "version": self._monotonic_version,
                "ts": time.time(),
                "status": self._status,
                "price": self._price,
                "analysis": self._analysis,
                "account": self._account,
                "structures": self._structures,
                "strength": self._strength,
                "correlation": self._correlation,
                "performance": self._performance,
                "calendar": self._calendar,
                "broker_meta": self._broker_meta,
            }


# Module-level singleton so the dashboard layer can import it without
# threading an instance through every callback. The analysis loop holds the
# same instance.
STATE: LatestState = LatestState()
