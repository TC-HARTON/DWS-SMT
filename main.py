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

    try:
        loop.start()
    except MT5ConnectionError as exc:
        log.error("Failed to initialise MT5: %s", exc)
        return 2

    # Inject per-symbol broker metadata (digits/point/pip_size) into the
    # shared state so the frontend can render spreads/SL/TP correctly.
    try:
        from analyzer.state import STATE
        STATE.set_broker_meta(connector.symbol_meta_dict())
    except Exception:  # noqa: BLE001 — never fatal
        log.exception("Failed to publish broker symbol meta")

    _install_signal_handlers(loop, stop)

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
