"""Serve the dashboard's WebSocket endpoint using flask-sock.

Browser side: ``dash_extensions.WebSocket`` component connects to this
URL and forwards each received message to clientside callbacks.
Server side: each accepted connection spawns a thread that blocks on
:meth:`LatestState.wait_for_update`, then sends the full serialised
snapshot. The condition-variable wake-up avoids polling and keeps
end-to-end latency well under the SPEC §19 100 ms target.

Pings
-----
``flask-sock`` does not emit application-level pings; instead the
underlying ``simple-websocket`` library answers low-level pings sent by
the browser. We additionally send a heartbeat snapshot every
``HEARTBEAT_INTERVAL_SEC`` even when state has not changed so that
intermediate proxies do not drop idle sockets.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

from flask_sock import Sock

from simple_websocket.errors import ConnectionClosed

import config
from analyzer.state import LatestState, STATE
from dashboard.serialize import snapshot_light, snapshot_to_json

if TYPE_CHECKING:
    from flask import Flask

log = logging.getLogger(__name__)


def mount_websocket(flask_app: "Flask", state: LatestState = STATE) -> Sock:
    """Attach the ``/ws`` route to *flask_app* and return the ``Sock`` handle."""
    sock = Sock(flask_app)

    @sock.route(config.DASH_WS_PATH)  # type: ignore[arg-type]
    def _ws_handler(ws):  # pragma: no cover — exercised via integration smoke test
        client = ws.environ.get("REMOTE_ADDR", "?")
        # Connect/disconnect is normal traffic and floods logs on monitoring
        # tools that reconnect each sample; keep at DEBUG so production INFO
        # stays meaningful.
        log.debug("WS client connected: %s", client)
        last_version = -1
        last_analysis_version = -1
        last_heartbeat = 0.0
        try:
            # Send an initial FULL snapshot so the UI populates immediately.
            payload = snapshot_to_json(state)
            ws.send(json.dumps(payload, separators=(",", ":")))
            last_version = payload["version"]
            last_analysis_version = state.analysis_version
            last_heartbeat = time.time()

            while True:
                got_new = state.wait_for_update(
                    last_version, timeout=config.WS_WAIT_TIMEOUT_SEC
                )
                now = time.time()

                if got_new:
                    # Full snapshot only when a heavy domain changed; otherwise
                    # a light price-only message (the client merges it).
                    av = state.analysis_version
                    if av > last_analysis_version:
                        # Recurring full: omit the static 2 MB oos_baseline (the
                        # client cached it from the initial snapshot below).
                        payload = snapshot_to_json(state, include_baseline=False)
                    else:
                        payload = snapshot_light(state)
                    if payload["version"] <= last_version:
                        continue
                    ws.send(json.dumps(payload, separators=(",", ":")))
                    last_version = payload["version"]
                    last_analysis_version = av
                    last_heartbeat = now
                elif now - last_heartbeat >= config.WS_HEARTBEAT_INTERVAL_SEC:
                    # Keep-alive: re-send a full snapshot (rare; also self-heals
                    # any client that missed a heavy update). Baseline omitted —
                    # the client already cached it from the initial snapshot.
                    payload = snapshot_to_json(state, include_baseline=False)
                    ws.send(json.dumps(payload, separators=(",", ":")))
                    last_version = payload["version"]
                    last_analysis_version = state.analysis_version
                    last_heartbeat = now
        except ConnectionClosed:
            # Normal client-initiated close — not an error.
            pass
        except Exception:  # noqa: BLE001 — log and let flask-sock close the socket
            log.exception("WS handler error for %s", client)
        finally:
            log.debug("WS client disconnected: %s", client)

    log.info("WebSocket route mounted at %s", config.DASH_WS_PATH)
    return sock
