"""Entrypoint: bring up the analysis loop, then serve the lightweight dashboard.

Run::

    python main.py

The default settings (host, port, MT5 path) come from ``config.py`` /
``.env``. CTRL+C performs a clean shutdown of the analysis loop and
closes the MT5 IPC handle.

The UI is a single static HTML + vanilla JS page (see ``static/``) that
opens a WebSocket to ``/ws`` and patches the DOM in place. Dash/React is
no longer in the request path — this is dramatically lighter and faster
than the Phase 1-5 stack.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import types

import config
from analyzer.analysis_loop import AnalysisLoop
from analyzer.logging_setup import configure_logging
from analyzer.mt5_connector import MT5ConnectionError, MT5Connector
from dashboard.lite_server import build_app

log = logging.getLogger(__name__)


def _install_signal_handlers(loop: AnalysisLoop, stop_event: threading.Event) -> None:
    """Translate SIGINT/SIGTERM into a clean stop_event + loop.stop()."""
    def handler(signum: int, _frame: types.FrameType | None) -> None:
        log.info("Received signal %s, shutting down", signal.Signals(signum).name)
        stop_event.set()
        loop.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):  # not main thread / not supported
            log.debug("Could not install handler for %s", sig)


def _bring_up_when_mt5_ready(
    loop: AnalysisLoop, connector: MT5Connector, stop: threading.Event,
) -> None:
    """Background bring-up: wait for MT5 to be reachable (retrying every
    ``config.MT5_RECONNECT_INTERVAL_SEC``), then start the analysis loop ONCE.

    The web server is already bound and serving before this runs, so the
    dashboard page always comes up — it shows the "MT5 offline / reconnecting"
    state (STATE defaults to ``connected=False``) until MT5 is ready, instead
    of the launcher refusing to start. This removes the old coupling where
    ``main`` exited (return 2) when MT5 was not yet up, leaving port 8050
    unbound and Dashboard.bat timing out after 60 s.
    """
    attempt = 0
    while not stop.is_set():
        attempt += 1
        try:
            connector.ensure_connected()   # the MT5-readiness gate; raises until ready
            break
        except MT5ConnectionError as exc:
            log.warning(
                "MT5 not ready (attempt %d): %s — retrying in %.0f s",
                attempt, exc, config.MT5_RECONNECT_INTERVAL_SEC,
            )
            if stop.wait(config.MT5_RECONNECT_INTERVAL_SEC):
                return
    if stop.is_set():
        return

    try:
        # MT5 is reachable now; start lines-watcher + warm-up + the loop.
        # (loop.start() re-calls connector.initialize(), which is idempotent.)
        loop.start()
    except MT5ConnectionError:
        # Rare race: MT5 dropped between the probe and warm-up. The loop's own
        # per-tick reconnect recovers once MT5 returns, so leave the server up.
        log.exception("MT5 dropped during analysis-loop start-up")
        return

    # Inject per-symbol broker metadata (digits/point/pip_size) into the
    # shared state so the frontend can render spreads/SL/TP correctly.
    try:
        from analyzer.state import STATE
        STATE.set_broker_meta(connector.symbol_meta_dict())
    except Exception:  # noqa: BLE001 — never fatal
        log.exception("Failed to publish broker symbol meta")


def main() -> int:
    configure_logging()

    log.info("=" * 70)
    log.info("Starting MT5-Python Trading Dashboard")
    log.info("MT5 terminal: %s", config.MT5_TERMINAL_PATH)
    log.info("Symbols: %s", ", ".join(s.base for s in config.SYMBOLS))
    log.info("Timeframes: %s", ", ".join(tf.label for tf in config.TIMEFRAMES))
    log.info("Dashboard will serve on http://%s:%d", config.DASH_HOST, config.DASH_PORT)
    log.info("=" * 70)

    connector = MT5Connector()
    loop = AnalysisLoop(connector)
    stop = threading.Event()

    _install_signal_handlers(loop, stop)

    # Bring MT5 + the analysis loop up in the BACKGROUND with retry, so the web
    # server below can bind port 8050 immediately. The dashboard then always
    # starts (and shows "reconnecting" until MT5 is ready) — Dashboard.bat no
    # longer times out when MT5 isn't running/ready at launch.
    threading.Thread(
        target=_bring_up_when_mt5_ready,
        args=(loop, connector, stop),
        name="mt5-bringup", daemon=True,
    ).start()

    app = build_app()

    try:
        app.run(
            host=config.DASH_HOST,
            port=config.DASH_PORT,
            debug=config.DASH_DEBUG,
            use_reloader=False,   # the analysis loop owns global state — do not double-spawn
            threaded=True,        # required so flask-sock can accept multiple WS clients
        )
    finally:
        if not stop.is_set():
            loop.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
