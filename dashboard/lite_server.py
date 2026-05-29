"""Lightweight HTML + WebSocket server (replaces Dash).

The Phase 1-5 Dash UI used React-driven callbacks. Phase 6 swaps that out
for a plain Flask app serving:

* a single static ``index.html`` (no framework, no React, no Plotly)
* a ``/ws`` WebSocket endpoint shipping the same JSON snapshot the old
  broadcaster sent, so the analyzer backend is untouched
* ``/static/*`` files (CSS + JS) cached forever — the client patches the
  DOM in-place via the WebSocket stream
* a ``/api/broker`` POST endpoint that flips the MT5 terminal in .env
  and triggers a self-restart so the user can switch brokers from the
  ACCOUNT badge dropdown without leaving the dashboard

This is dramatically lighter than Dash: ~30 KB total payload, zero
clientside framework overhead, direct DOM updates only on the cells
that actually changed.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from flask import Flask, jsonify, request, send_from_directory

import config
from dashboard.ws_broadcaster import mount_websocket

log = logging.getLogger(__name__)

_STATIC_DIR = config.PROJECT_ROOT / "static"
_ENV_FILE = config.PROJECT_ROOT / ".env"
_RESTART_SCRIPT = config.PROJECT_ROOT / "Dashboard.bat"


def _rewrite_env_terminal_path(new_path: str) -> None:
    """Replace (or append) the MT5_TERMINAL_PATH line in .env, preserving
    every other line.

    Reads as bytes and strips a leading UTF-8 BOM if present — earlier
    revisions accidentally introduced one (PowerShell's
    ``Set-Content -Encoding utf8`` writes a BOM, and python-dotenv then
    reads the first key as ``\\ufeffFRED_API_KEY`` so the var never lands
    in ``os.environ``, breaking every dotenv-keyed setting downstream).
    Writes back without a BOM via ``encoding='utf-8'`` (Python's default).
    """
    raw = b""
    if _ENV_FILE.exists():
        raw = _ENV_FILE.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    target = f"MT5_TERMINAL_PATH={new_path}"
    replaced = False
    for i, line in enumerate(lines):
        if line.startswith("MT5_TERMINAL_PATH="):
            lines[i] = target
            replaced = True
            break
    if not replaced:
        lines.append(target)
    _ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _spawn_restart_then_exit(new_terminal_path: str) -> None:
    """Detach a python relaunch and exit this process.

    Sleeps briefly so the HTTP response reaches the browser, then asks
    PowerShell to wait 3 s (long enough for our listen socket to fully
    release AND for the browser's reconnect loop to start retrying) and
    then launch ``python.exe main.py`` in a new visible console. Then
    calls ``os._exit`` — Werkzeug's dev server has no usable graceful
    shutdown handshake.

    We pass ``MT5_TERMINAL_PATH`` to the child explicitly via ``env=``
    because ``python-dotenv``'s ``load_dotenv()`` is non-overriding by
    default: the dying process's environment already contains the OLD
    path (loaded at startup), and that would shadow the just-rewritten
    ``.env`` line in the new process. Setting it explicitly forces the
    child to see the new broker.

    History: an earlier revision tried to chain ``timeout ... & start ...``
    through ``subprocess.Popen([...], shell=False)``; the shell operators
    ``>``/``&`` became literal arguments under that form, so the chain
    never ran and the dashboard froze on the "Switching…" overlay forever.
    A second revision went through Dashboard.bat, but its ``start "TITLE"
    cmd /k …`` pattern doesn't survive a PowerShell → cmd → start chain
    reliably when spawned in detached mode. Launching python directly is
    simpler and avoids both quoting traps.
    """
    import sys
    import tempfile
    import time
    time.sleep(0.4)   # let the broker-switch HTTP response reach the browser

    python_exe = sys.executable
    project_root = str(config.PROJECT_ROOT)
    # Robust relaunch: write the steps to a plain-text .bat on disk and launch
    # THAT — instead of an inline nested ``cmd /c start … cmd /c "…"`` string.
    # The previous inline form nested quotes (``"{project_root}"`` /
    # ``"{python_exe}"`` inside an already-quoted ``"{inner}"``); under
    # CreateProcess those mangled and the relaunch SILENTLY never started,
    # taking the dashboard down on a broker switch. A .bat file is plain text
    # with no quote-nesting to mangle.
    #
    # The .bat sets the new broker path (python-dotenv's load_dotenv is
    # non-overriding, so an env var shadows .env), waits ~3 s for :8050 to
    # release, then opens the familiar "MT5 Dashboard" console (same shape as
    # Dashboard.bat). The outer ``cmd /c start`` makes the OS shell own the
    # child so it survives this process's os._exit.
    bat = (
        "@echo off\r\n"
        f'set "MT5_TERMINAL_PATH={new_terminal_path}"\r\n'
        f'cd /d "{project_root}"\r\n'
        "ping -n 4 127.0.0.1 >nul\r\n"
        f'start "MT5 Dashboard" cmd /k "{python_exe}" main.py\r\n'
    )
    bat_path = os.path.join(tempfile.gettempdir(), "mt5_broker_switch_relaunch.bat")
    try:
        with open(bat_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(bat)
    except OSError:
        log.exception("broker-switch: could not write relaunch .bat — staying up")
        return                      # do NOT exit without a guaranteed relaunch
    DETACHED_PROCESS = 0x00000008
    try:
        # Args as a LIST so Windows does not re-parse/mangle the quoting.
        subprocess.Popen(
            ["cmd", "/c", "start", "", "/MIN", bat_path],
            cwd=project_root,
            creationflags=DETACHED_PROCESS,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        log.exception("broker-switch restart spawn failed — staying up")
        return                      # do NOT exit if the relaunch did not spawn
    log.info("broker-switch: relaunch .bat spawned; exiting so it can bind 8050 in ~3 s")
    os._exit(0)


def build_app() -> Flask:
    """Construct the Flask app, mount static + ``/ws``, return it."""
    app = Flask(
        __name__,
        static_folder=str(_STATIC_DIR),
        static_url_path="/static",
    )

    @app.route("/")
    def index():  # pragma: no cover — exercised via the smoke verifier
        return send_from_directory(str(_STATIC_DIR), "index.html")

    @app.route("/favicon.ico")
    def favicon():  # pragma: no cover
        return ("", 204)

    @app.route("/api/broker", methods=["GET"])
    def get_broker():
        return jsonify({
            "current_path": config.MT5_TERMINAL_PATH,
            "presets": config.BROKER_PRESETS,
        })

    @app.route("/api/broker", methods=["POST"])
    def post_broker():
        body = request.get_json(silent=True) or {}
        name = body.get("name")
        if name not in config.BROKER_PRESETS:
            return jsonify({"ok": False,
                            "error": f"unknown broker {name!r}"}), 400
        new_path = config.BROKER_PRESETS[name]
        try:
            _rewrite_env_terminal_path(new_path)
        except OSError as exc:
            log.exception(".env rewrite failed")
            return jsonify({"ok": False, "error": str(exc)}), 500
        log.info("broker-switch requested: %s → %s", name, new_path)
        # Detach the restart so we can return the HTTP response first.
        threading.Thread(
            target=_spawn_restart_then_exit,
            args=(new_path,),
            daemon=True,
        ).start()
        return jsonify({"ok": True, "name": name, "path": new_path})

    mount_websocket(app)
    log.info(
        "Lite dashboard built on http://%s:%d (static=%s, WS=%s)",
        config.DASH_HOST, config.DASH_PORT, _STATIC_DIR, config.DASH_WS_PATH,
    )
    return app
