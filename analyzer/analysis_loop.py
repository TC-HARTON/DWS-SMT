"""Background loop that polls MT5, computes indicators, and updates shared state.

The loop runs on its own daemon thread and drives several independent
schedules sourced from :mod:`config`:

* ``PRICE_REFRESH_SEC`` (1 s): tick + account refresh (SPEC §14.4).
* ``ANALYSIS_REFRESH_SEC`` (5 s): full indicator recomputation (SPEC §19).

Cold-cache mitigation
---------------------
The MetaTrader 5 IPC reports rates from local cache when available, but
the very first ``copy_rates_from_pos`` for a freshly-selected symbol
synchronously fetches history from the broker, which we measured at ~50 s
across all 40 (symbol, TF) pairs at start-up. To keep the UI responsive
we run a synchronous warm-up pass inside :meth:`AnalysisLoop.start` before
the first 5-second tick fires.

Reconnection
------------
Per SPEC §18.4, the loop attempts MT5 reconnection every
``MT5_RECONNECT_INTERVAL_SEC`` when it sees an :class:`MT5ConnectionError`.
The shared state's ``ConnectionStatus`` is updated so the dashboard can
render a "disconnected" banner.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import config
from analyzer import dxy_feed, ema_stack
from analyzer.account_monitor import PerformanceEngine
from analyzer.calendar_feed import CalendarEngine
from analyzer.cot_feed import CotEngine
from analyzer.indicator_engine import IndicatorEngine
from analyzer.macro_feed import MacroEngine
from analyzer.mt5_connector import MT5Connector, MT5ConnectionError
from analyzer.state import (
    ConnectionStatus,
    LatestState,
    PriceSnapshot,
    STATE,
)

log = logging.getLogger(__name__)


@dataclass
class _Schedule:
    """Bookkeeping for one periodic job within the unified loop."""

    name: str
    interval: float
    next_run: float = 0.0  # epoch seconds; 0 means "due immediately"


class AnalysisLoop:
    """Drive several periodic schedules from a single daemon thread."""

    def __init__(
        self,
        connector: MT5Connector,
        engine: IndicatorEngine | None = None,
        state: LatestState = STATE,
        performance_engine: PerformanceEngine | None = None,
        calendar_engine: CalendarEngine | None = None,
        macro_engine: MacroEngine | None = None,
        cot_engine: CotEngine | None = None,
        price_interval: float = config.PRICE_REFRESH_SEC,
        analysis_interval: float = config.ANALYSIS_REFRESH_SEC,
        history_interval: float = config.HISTORY_REFRESH_SEC,
        calendar_interval: float = config.CALENDAR_REFRESH_SEC,
        macro_interval: float = config.MACRO_REFRESH_SEC,
        realyield_interval: float = config.MACRO_REALYIELD_REFRESH_SEC,
        realyield_live_interval: float = config.MACRO_REALYIELD_LIVE_REFRESH_SEC,
        cot_interval: float = config.COT_REFRESH_SEC,
        reconnect_interval: float = config.MT5_RECONNECT_INTERVAL_SEC,
    ) -> None:
        self._connector = connector
        self._engine = engine or IndicatorEngine()
        self._state = state
        self._performance_engine = performance_engine or PerformanceEngine(connector)
        self._calendar_engine = calendar_engine or CalendarEngine()
        # Calendar HTTP fetch runs off-thread because the upstream timeout
        # is up to 30 s on a network outage — blocking the analysis loop
        # would starve the 1 s price refresh (SPEC §14.4).
        self._calendar_inflight = threading.Event()
        # Macro fetch is pure HTTP (no MT5 connector lock), so it cannot starve
        # the price tick — off-thread only keeps a slow network call out of the
        # loop.
        self._macro_engine = macro_engine or MacroEngine()
        self._macro_inflight = threading.Event()
        # The real yield moves daily, so it refreshes faster than policy rates
        # (spec §B.11). Same MacroEngine, separate in-flight guard + schedule.
        self._realyield_inflight = threading.Event()
        self._realyield_live_inflight = threading.Event()
        # COT (gold-positioning) is weekly data over plain HTTP — same off-thread
        # treatment as macro: it can't starve the price tick, the worker just
        # keeps a slow network call out of the loop.
        self._cot_engine = cot_engine or CotEngine()
        self._cot_inflight = threading.Event()
        self._reconnect_interval = reconnect_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # TFs fetched each analysis cycle = the indicator timeframes (D1/H4/H1/M15).
        self._all_tfs = tuple(config.TIMEFRAMES)
        self._schedules = (
            _Schedule("price", price_interval),
            _Schedule("analysis", analysis_interval),
            _Schedule("history", history_interval),
            _Schedule("calendar", calendar_interval),
            _Schedule("macro", macro_interval),
            _Schedule("realyield", realyield_interval),
            _Schedule("realyield_live", realyield_live_interval),
            _Schedule("cot", cot_interval),
        )

    # --------------------------------------------------------------- start
    def start(self) -> None:
        """Initialise MT5, run warm-up, then spin up the loop."""
        self._connector.initialize()
        self._mark_status(connected=True, error=None)
        self._warmup()
        self._thread = threading.Thread(
            target=self._run, name="mt5-analysis-loop", daemon=True
        )
        self._thread.start()
        log.info("Analysis loop started")

    def stop(self, join_timeout: float = 5.0) -> None:
        """Signal the loop to exit and tear down MT5."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(join_timeout)
        self._connector.shutdown()
        log.info("Analysis loop stopped")

    # --------------------------------------------------------------- main
    def _run(self) -> None:
        bases = list(self._connector.resolved_symbols.keys())
        while not self._stop.is_set():
            now = time.time()
            next_event = now + 1.0
            for sched in self._schedules:
                if sched.next_run <= now:
                    self._dispatch(sched.name, bases)
                    # Anchor the schedule to wall-clock multiples of its interval
                    # so a slow tick doesn't drift the cadence indefinitely.
                    sched.next_run = now + sched.interval
                if sched.next_run < next_event:
                    next_event = sched.next_run

            sleep_for = max(0.05, next_event - time.time())
            if self._stop.wait(sleep_for):
                return

    # --------------------------------------------------------- dispatcher
    def _dispatch(self, name: str, bases: list[str]) -> None:
        handler = {
            "price": self._do_price_refresh,
            "analysis": self._do_analysis_refresh,
            "history": self._do_history_refresh,
            "calendar": self._do_calendar_refresh,
            "macro": self._do_macro_refresh,
            "realyield": self._do_realyield_refresh,
            "realyield_live": self._do_realyield_live_refresh,
            "cot": self._do_cot_refresh,
        }[name]
        try:
            handler(bases)
        except MT5ConnectionError as exc:
            log.warning("MT5 connection lost during %s tick: %s", name, exc)
            self._mark_status(connected=False, error=str(exc))
            self._attempt_reconnect()
        except Exception:  # noqa: BLE001 — never let the loop die silently
            log.exception("Unhandled error in %s tick", name)

    # ---------------------------------------------------------- handlers
    def _do_price_refresh(self, bases: list[str]) -> None:
        ticks = self._connector.latest_ticks(bases)
        if not ticks:
            return
        self._state.set_price(PriceSnapshot(generated_at=time.time(), ticks=ticks))
        # SPEC §14.4 also refreshes account at 1 s.
        account = self._connector.account_snapshot()
        if account is not None:
            self._state.set_account(account)
        if not self._state.status.connected:
            self._mark_status(connected=True, error=None)

    def _do_analysis_refresh(self, bases: list[str]) -> None:
        self._run_analysis_pass(bases)

    def _run_analysis_pass(self, bases: list[str]) -> int:
        """Fetch rates and compute the indicator snapshot.

        Returns the number of ``(symbol, TF)`` pairs that produced an
        indicator snapshot (useful for warm-up logging).
        """
        rates = self._connector.fetch_rates_parallel(bases, self._all_tfs)
        if not rates:
            return 0
        # Indicator engine only uses TIMEFRAMES from the dict; STRUCTURE_TFS
        # are silently ignored by it.
        snap = self._engine.compute(rates)
        snap = IndicatorEngine.with_broker_names(snap, self._connector.resolved_symbols)
        self._state.set_analysis(snap)

        # DXY dollar-context: light (one copy_rates), so piggyback the analysis
        # cadence. Guarded so a DXY failure never reaches the loop dispatcher.
        try:
            self._state.set_dxy(dxy_feed.compute_dxy(self._connector))
        except Exception:  # noqa: BLE001
            log.exception("dxy compute failed")

        # EMA-stack oscillator — compute every configured mode (M15 + H1), each
        # guarded independently so one mode failing leaves the other live.
        for _spec in config.EMA_STACK_MODES:
            try:
                self._state.set_ema_stack(
                    ema_stack.compute_ema_stack_for(self._connector, _spec))
            except Exception:  # noqa: BLE001
                log.exception("ema_stack compute failed (%s)", _spec.name)

        return len(rates)

    def _do_history_refresh(self, bases: list[str]) -> None:
        """SPEC §14.4: trade-history refresh every 60 s → performance stats."""
        performance = self._performance_engine.compute()
        self._state.set_performance(performance)

    def _do_calendar_refresh(self, bases: list[str]) -> None:
        """SPEC §15.4: economic-calendar XML refresh every 1 hour.

        Dispatched to a daemon worker so the up-to-30s HTTP timeout never
        blocks the 1 s price tick (SPEC §14.4). At most one fetch runs at a
        time — overlapping cycles re-use the in-flight result.
        """
        if self._calendar_inflight.is_set():
            log.debug("calendar: previous fetch still in flight, skipping tick")
            return
        self._calendar_inflight.set()
        worker = threading.Thread(
            target=self._calendar_refresh_worker,
            name="calendar-fetch", daemon=True,
        )
        worker.start()

    def _calendar_refresh_worker(self) -> None:
        try:
            snap = self._calendar_engine.compute()
            self._state.set_calendar(snap)
        except Exception:               # noqa: BLE001 — never propagate to the loop
            log.exception("calendar worker failed")
        finally:
            self._calendar_inflight.clear()

    def _do_macro_refresh(self, bases: list[str]) -> None:
        """Spec Section B: refresh central-bank rates + employment every 6 h.

        Dispatched to a daemon worker — the HTTP fetch can take up to
        MACRO_FETCH_TIMEOUT_SEC per source. The fetch is plain HTTP and never
        touches the MT5 connector, so it cannot starve the price tick; the
        off-thread dispatch only keeps a slow network call out of the loop.
        """
        if self._macro_inflight.is_set():
            log.debug("macro: previous fetch still in flight, skipping tick")
            return
        self._macro_inflight.set()
        worker = threading.Thread(
            target=self._macro_refresh_worker,
            name="macro-fetch", daemon=True,
        )
        worker.start()

    def _macro_refresh_worker(self) -> None:
        try:
            snap = self._macro_engine.compute()
            self._state.set_macro(snap)
            if snap.last_error:     # a source failed → retry soon, not in 6 h
                self._reschedule_soon("macro", config.MACRO_RETRY_SEC)
        except Exception:               # noqa: BLE001 — never reach the loop
            log.exception("macro worker failed")
            self._reschedule_soon("macro", config.MACRO_RETRY_SEC)
        finally:
            self._macro_inflight.clear()

    def _do_realyield_refresh(self, bases: list[str]) -> None:
        """Spec §B.11: refresh the US 10Y real yield hourly (a daily-moving
        market signal, faster cadence than the 6 h policy-rate refresh)."""
        if self._realyield_inflight.is_set():
            log.debug("realyield: previous fetch still in flight, skipping tick")
            return
        self._realyield_inflight.set()
        worker = threading.Thread(
            target=self._realyield_refresh_worker,
            name="realyield-fetch", daemon=True,
        )
        worker.start()

    def _realyield_refresh_worker(self) -> None:
        try:
            snap = self._macro_engine.fetch_real_yield()
            self._state.set_real_yield(snap)
            if snap.stale:          # fetch failed (served stale) → retry soon
                self._reschedule_soon("realyield", config.MACRO_RETRY_SEC)
        except Exception:               # noqa: BLE001 — never reach the loop
            log.exception("realyield worker failed")
            self._reschedule_soon("realyield", config.MACRO_RETRY_SEC)
        finally:
            self._realyield_inflight.clear()

    def _do_realyield_live_refresh(self, bases: list[str]) -> None:
        """Real-time real-yield overlay every ~30 s: the daily DFII10 anchor +
        the intraday move in the nominal 10Y (^TNX). Plain HTTP, off-thread —
        gives the panel live movement while the Treasury market is open."""
        if self._realyield_live_inflight.is_set():
            return
        self._realyield_live_inflight.set()
        worker = threading.Thread(
            target=self._realyield_live_refresh_worker,
            name="realyield-live-fetch", daemon=True,
        )
        worker.start()

    def _realyield_live_refresh_worker(self) -> None:
        try:
            snap = self._macro_engine.fetch_real_yield_live()
            self._state.set_real_yield(snap)
        except Exception:               # noqa: BLE001 — never reach the loop
            log.exception("realyield-live worker failed")
        finally:
            self._realyield_live_inflight.clear()

    def _do_cot_refresh(self, bases: list[str]) -> None:
        """Refresh the CFTC COT gold-positioning snapshot every 6 h.

        Dispatched to a daemon worker — the fetch is plain HTTP (no MT5 lock),
        so it cannot starve the price tick; off-thread only keeps a slow network
        call out of the loop. At most one fetch runs at a time."""
        if self._cot_inflight.is_set():
            log.debug("cot: previous fetch still in flight, skipping tick")
            return
        self._cot_inflight.set()
        worker = threading.Thread(
            target=self._cot_refresh_worker,
            name="cot-fetch", daemon=True,
        )
        worker.start()

    def _cot_refresh_worker(self) -> None:
        try:
            snap = self._cot_engine.compute()
            self._state.set_cot(snap)
            if snap.stale:          # fetch failed (served stale) → retry soon
                self._reschedule_soon("cot", config.MACRO_RETRY_SEC)
        except Exception:               # noqa: BLE001 — never reach the loop
            log.exception("cot worker failed")
            self._reschedule_soon("cot", config.MACRO_RETRY_SEC)
        finally:
            self._cot_inflight.clear()

    def _reschedule_soon(self, name: str, delay: float) -> None:
        """Pull a schedule's next run forward to ``now + delay`` so a failed
        macro / real-yield fetch retries in minutes instead of the full 6 h / 1 h
        interval. Only ever moves the run EARLIER, never later."""
        target = time.time() + delay
        for sched in self._schedules:
            if sched.name == name and target < sched.next_run:
                sched.next_run = target
                log.info("%s: fetch failed — retry in %.0f s (not full interval)",
                         name, delay)
                return

    # ------------------------------------------------------------- warmup
    def _warmup(self) -> None:
        """Prime MT5's per-symbol history cache to avoid a 50 s first analysis tick."""
        t0 = time.perf_counter()
        bases = list(self._connector.resolved_symbols.keys())
        n_rates = self._run_analysis_pass(bases)
        # Also seed the price snapshot so the dashboard renders something immediately.
        ticks = self._connector.latest_ticks(bases)
        if ticks:
            self._state.set_price(PriceSnapshot(generated_at=time.time(), ticks=ticks))
        account = self._connector.account_snapshot()
        if account is not None:
            self._state.set_account(account)
        log.info(
            "Warm-up complete in %.2f s (rates=%d, ticks=%d, account=%s)",
            time.perf_counter() - t0, n_rates, len(ticks or {}),
            "yes" if account is not None else "no",
        )

    # -------------------------------------------------------- reconnect
    def _attempt_reconnect(self) -> None:
        """Block this thread until the connection is restored or stop is signalled."""
        while not self._stop.is_set():
            time.sleep(self._reconnect_interval)
            try:
                self._connector.ensure_connected()
                self._mark_status(connected=True, error=None)
                log.info("MT5 reconnected")
                return
            except MT5ConnectionError as exc:
                self._mark_status(connected=False, error=str(exc))
                log.warning("MT5 reconnect attempt failed: %s", exc)

    def _mark_status(self, connected: bool, error: str | None) -> None:
        self._state.set_status(
            ConnectionStatus(
                connected=connected,
                last_error=error,
                last_connect_ts=time.time() if connected else None,
            )
        )
