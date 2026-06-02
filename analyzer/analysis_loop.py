"""Background loop that polls MT5, computes indicators, and updates shared state.

The loop runs on its own daemon thread and drives three independent
schedules sourced from :mod:`config`:

* ``PRICE_REFRESH_SEC`` (1 s): tick + account refresh (SPEC §14.4).
* ``ANALYSIS_REFRESH_SEC`` (5 s): full indicator recomputation (SPEC §19).
* ``HEAVY_REFRESH_SEC`` (30 s): currency strength / heavy passes
  (placeholder hook; Phase 3 logic plugs in here).

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

import pandas as pd

import config
from analyzer import confluence, gold_macro, price_action, structure_detector, trigger_store
from analyzer.account_monitor import PerformanceEngine
from analyzer.calendar_feed import CalendarEngine
from analyzer.correlation import CorrelationEngine
from analyzer.currency_strength import CurrencyStrengthEngine
from analyzer.indicator_engine import IndicatorEngine
from analyzer.line_reader import LINES, LinesState, LinesWatcher
from analyzer.signal_validator import SignalValidator
from analyzer.macro_feed import MacroEngine
from analyzer.mt5_connector import MT5Connector, MT5ConnectionError
# Pattern matcher live pipeline is currently decommissioned — the front-end
# does not consume the results so running it every cycle wastes CPU + WS
# bandwidth. ``analyzer.pattern_matcher`` and the JSON tables under
# ``data/loss_analysis/`` are kept for future walk-forward-validated
# re-introduction; re-enable by re-importing PatternMatcher here and
# restoring ``_publish_pattern_matches`` in ``_run_analysis_pass``.
from analyzer.state import (
    ConnectionStatus,
    LatestState,
    PriceSnapshot,
    STATE,
    StructuresSnapshot,
    SymbolStructures,
)

log = logging.getLogger(__name__)


@dataclass
class _Schedule:
    """Bookkeeping for one periodic job within the unified loop."""

    name: str
    interval: float
    next_run: float = 0.0  # epoch seconds; 0 means "due immediately"


class AnalysisLoop:
    """Drive three periodic schedules from a single daemon thread."""

    def __init__(
        self,
        connector: MT5Connector,
        engine: IndicatorEngine | None = None,
        state: LatestState = STATE,
        lines_state: LinesState = LINES,
        lines_watcher: LinesWatcher | None = None,
        strength_engine: CurrencyStrengthEngine | None = None,
        correlation_engine: CorrelationEngine | None = None,
        performance_engine: PerformanceEngine | None = None,
        calendar_engine: CalendarEngine | None = None,
        signal_validator: SignalValidator | None = None,
        macro_engine: MacroEngine | None = None,
        price_interval: float = config.PRICE_REFRESH_SEC,
        analysis_interval: float = config.ANALYSIS_REFRESH_SEC,
        heavy_interval: float = config.HEAVY_REFRESH_SEC,
        history_interval: float = config.HISTORY_REFRESH_SEC,
        calendar_interval: float = config.CALENDAR_REFRESH_SEC,
        validation_interval: float = config.VALIDATION_REFRESH_SEC,
        macro_interval: float = config.MACRO_REFRESH_SEC,
        realyield_interval: float = config.MACRO_REALYIELD_REFRESH_SEC,
        goldmacro_interval: float = config.GOLD_MACRO_REFRESH_SEC,
        reconnect_interval: float = config.MT5_RECONNECT_INTERVAL_SEC,
    ) -> None:
        self._connector = connector
        self._engine = engine or IndicatorEngine()
        self._state = state
        self._lines_state = lines_state
        self._lines_watcher = lines_watcher or LinesWatcher(state=lines_state)
        self._strength_engine = strength_engine or CurrencyStrengthEngine(connector)
        self._correlation_engine = correlation_engine or CorrelationEngine(connector)
        self._performance_engine = performance_engine or PerformanceEngine(connector)
        self._calendar_engine = calendar_engine or CalendarEngine()
        # Calendar HTTP fetch runs off-thread because the upstream timeout
        # is up to 30 s on a network outage — blocking the analysis loop
        # would starve the 1 s price refresh (SPEC §14.4).
        self._calendar_inflight = threading.Event()
        self._signal_validator = signal_validator or SignalValidator(connector)
        # Deep-history validation runs off-thread — the parallel fetch of
        # VALIDATION_HISTORY_BARS across every symbol/TF takes far longer than
        # the 0.5 s price tick may wait.
        self._validation_inflight = threading.Event()
        # Macro fetch is pure HTTP (no MT5 connector lock), so it cannot starve
        # the price tick the way the validation worker can — off-thread only
        # keeps a slow network call out of the loop.
        self._macro_engine = macro_engine or MacroEngine()
        self._macro_inflight = threading.Event()
        # The real yield moves daily, so it refreshes faster than policy rates
        # (spec §B.11). Same MacroEngine, separate in-flight guard + schedule.
        self._realyield_inflight = threading.Event()
        # GoldMacroScore drivers are daily-moving (same cadence as the real
        # yield); same MacroEngine, separate in-flight guard + schedule.
        self._goldmacro_inflight = threading.Event()
        self._reconnect_interval = reconnect_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Pre-compute the full TF set we hand to fetch_rates_parallel each
        # cycle: the Phase-1 indicator TFs plus the Phase-2 structure TFs.
        # We dedupe by label so a future config edit that promotes a TF from
        # STRUCTURE_TFS to TIMEFRAMES (or vice-versa) does not silently send
        # two entries with the same key to the connector.
        seen_labels: set[str] = set()
        unique_tfs: list[config.TimeframeSpec] = []
        for tf in (*config.TIMEFRAMES, *config.STRUCTURE_TFS):
            if tf.label in seen_labels:
                continue
            seen_labels.add(tf.label)
            unique_tfs.append(tf)
        self._all_tfs = tuple(unique_tfs)
        self._schedules = (
            _Schedule("price", price_interval),
            _Schedule("analysis", analysis_interval),
            _Schedule("heavy", heavy_interval),
            _Schedule("history", history_interval),
            _Schedule("calendar", calendar_interval),
            # Delay the first validation pass: its deep-history fetch is heavy,
            # so let warm-up and the first normal cycles settle first.
            _Schedule("validation", validation_interval,
                      next_run=time.time() + config.VALIDATION_STARTUP_DELAY_SEC),
            _Schedule("macro", macro_interval),
            _Schedule("realyield", realyield_interval),
            _Schedule("goldmacro", goldmacro_interval),
        )

    # --------------------------------------------------------------- start
    def start(self) -> None:
        """Initialise MT5, start the lines watcher, run warm-up, then spin up the loop."""
        self._connector.initialize()
        self._mark_status(connected=True, error=None)
        self._lines_watcher.start()
        # Resolve Phase-3 currency-strength crosses (skips broker-missing ones).
        self._strength_engine.resolve_pairs()
        self._warmup()
        self._thread = threading.Thread(
            target=self._run, name="mt5-analysis-loop", daemon=True
        )
        self._thread.start()
        log.info("Analysis loop started")

    def stop(self, join_timeout: float = 5.0) -> None:
        """Signal the loop to exit and tear down MT5 + lines watcher."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(join_timeout)
        try:
            self._lines_watcher.stop()
        except Exception:  # noqa: BLE001 — keep tear-down going
            log.exception("LinesWatcher.stop raised")
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
            "heavy": self._do_heavy_refresh,
            "history": self._do_history_refresh,
            "calendar": self._do_calendar_refresh,
            "validation": self._do_validation_refresh,
            "macro": self._do_macro_refresh,
            "realyield": self._do_realyield_refresh,
            "goldmacro": self._do_goldmacro_refresh,
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
        """Fetch rates, compute indicators + structure + PA + confluence.

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

        self._publish_structures(bases, rates, snap)
        return len(rates)

    # ---------------------------------------------------- structure pass
    def _publish_structures(
        self,
        bases: list[str],
        rates: dict[tuple[str, str], pd.DataFrame],
        snap,
    ) -> None:
        """Build a :class:`StructuresSnapshot` and publish it to shared state."""
        price_snapshot = self._state.price
        ticks = price_snapshot.ticks if price_snapshot else {}
        by_symbol: dict[str, SymbolStructures] = {}

        for base in bases:
            sym_rates = {
                tf_label: df for (b, tf_label), df in rates.items() if b == base
            }
            if not sym_rates:
                continue

            current_price = self._current_price(base, ticks, sym_rates)

            ea_levels = self._lines_state.levels_for(base)
            auto_levels = structure_detector.detect_all(
                base, sym_rates, current_price=current_price,
            )
            all_levels = tuple(ea_levels + auto_levels)

            m15 = sym_rates.get("M15")
            pa_events = (tuple(price_action.detect_all(m15))
                         if m15 is not None and not m15.empty else ())

            atr_h4 = self._h4_atr(snap, base)
            clusters = tuple(confluence.detect(
                list(all_levels), atr_h4=atr_h4, current_price=current_price,
            ))

            by_symbol[base] = SymbolStructures(
                levels=all_levels,
                price_action=pa_events,
                confluences=clusters,
            )

        self._state.set_structures(StructuresSnapshot(
            generated_at=time.time(),
            by_symbol=by_symbol,
        ))

    @staticmethod
    def _current_price(base, ticks, sym_rates) -> float | None:
        tick = ticks.get(base)
        if tick is not None and tick.bid:
            return tick.bid
        m15 = sym_rates.get("M15")
        if m15 is not None and not m15.empty:
            return float(m15["close"].iloc[-1])
        return None

    @staticmethod
    def _h4_atr(snap, base: str) -> float | None:
        sym_ind = snap.by_symbol.get(base)
        if sym_ind is None:
            return None
        h4 = sym_ind.by_tf.get("H4")
        return h4.atr if h4 is not None else None

    def _do_heavy_refresh(self, bases: list[str]) -> None:
        """SPEC Phase 3: currency strength (§12) + correlation matrix (§13)."""
        strength = self._strength_engine.compute()
        if strength.by_window:
            self._state.set_strength(strength)
        correlation = self._correlation_engine.compute()
        if correlation.by_window:
            self._state.set_correlation(correlation)

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

    def _do_validation_refresh(self, bases: list[str]) -> None:
        """Spec Section A: deep-history signal validation every 5 minutes.

        Dispatched to a daemon worker — the parallel deep-history fetch is far
        too slow to run inside the loop without starving the 0.5 s price tick.
        At most one validation runs at a time; overlapping cycles are skipped.

        Validation covers only the DWS-SMT display symbols (``config.SYMBOLS``):
        *bases* also carries the currency-strength cross pairs, but those have
        no DWS panel — validating them is pure wasted fetch + compute load.
        """
        if self._validation_inflight.is_set():
            log.debug("validation: previous pass still in flight, skipping tick")
            return
        self._validation_inflight.set()
        display_bases = [s.base for s in config.SYMBOLS]
        worker = threading.Thread(
            target=self._validation_refresh_worker,
            args=(display_bases,),
            name="signal-validation", daemon=True,
        )
        worker.start()

    def _validation_refresh_worker(self, bases: list[str]) -> None:
        try:
            snap = self._signal_validator.compute(bases, self._state.broker_meta)
            self._state.set_validation(snap)
            self._persist_live_triggers(snap)
        except Exception:               # noqa: BLE001 — never reach the loop
            log.exception("signal-validation worker failed")
        finally:
            self._validation_inflight.clear()

    def _persist_live_triggers(self, snap) -> None:
        """Append this pass's CLOSED live triggers to the per-broker on-disk
        store, then publish the accumulated per-(symbol, TF) year history so the
        dashboard shows the COMPLETE live record — not just the broker's sliding
        window. Keyed by the connected broker (MT5 server)."""
        acct = self._state.account
        server = acct.server if acct is not None else None
        if not server:
            return    # no broker identity yet — skip until the account arrives
        history: dict[str, dict[str, dict]] = {}
        total_new = 0
        for sym, per_tf in snap.by_symbol.items():
            tf_hist: dict[str, dict] = {}
            for tf, stats in per_tf.items():
                triggers = getattr(stats.raw, "recent_triggers", ()) or ()
                total_new += trigger_store.append_closed(server, sym, tf, triggers)
                tf_hist[tf] = trigger_store.load_by_year(server, sym, tf)
            history[sym] = tf_hist
        if total_new:
            log.info("live trigger store: +%d new closed triggers (%s)",
                     total_new, server)
        self._state.set_live_trigger_history(history, server)

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

    def _do_goldmacro_refresh(self, bases: list[str]) -> None:
        """Refresh the GoldMacroScore hourly (its FRED drivers move daily)."""
        if self._goldmacro_inflight.is_set():
            log.debug("goldmacro: previous fetch still in flight, skipping tick")
            return
        self._goldmacro_inflight.set()
        worker = threading.Thread(
            target=self._goldmacro_refresh_worker,
            name="goldmacro-fetch", daemon=True,
        )
        worker.start()

    def _goldmacro_refresh_worker(self) -> None:
        try:
            histories, as_of, n_fresh = self._macro_engine.fetch_gold_drivers()
            # stale = at least one driver was served from cache (or all were):
            # the composite is still produced, but flagged and retried soon so
            # full fresh coverage returns without waiting a whole interval.
            stale = n_fresh < len(gold_macro.GOLD_DRIVERS)
            snap = gold_macro.compute_gold_macro_score(
                histories, as_of=as_of, stale=stale,
            )
            self._state.set_gold_macro(snap)
            if stale:               # not all drivers fresh → restore coverage soon
                self._reschedule_soon("goldmacro", config.MACRO_RETRY_SEC)
        except Exception:               # noqa: BLE001 — never reach the loop
            log.exception("goldmacro worker failed")
            self._reschedule_soon("goldmacro", config.MACRO_RETRY_SEC)
        finally:
            self._goldmacro_inflight.clear()

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
