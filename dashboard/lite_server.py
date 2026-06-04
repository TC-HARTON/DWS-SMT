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
import time
from urllib.parse import urlparse
from flask import Flask, jsonify, request, send_from_directory

import config
from analyzer import journal_store
from analyzer.state import STATE
from dashboard.ws_broadcaster import mount_websocket

log = logging.getLogger(__name__)

_STATIC_DIR = config.PROJECT_ROOT / "static"
_ENV_FILE = config.PROJECT_ROOT / ".env"

# Hosts that count as "this machine" for the broker-switch same-origin guard.
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def _origin_is_local(origin: str | None) -> bool:
    """True if the request ``Origin`` is absent or points at this machine.

    A cross-site page loaded in the user's browser cannot forge a local
    ``Origin`` header, so requiring one blocks CSRF-style broker switches
    (which restart the process) while leaving the dashboard's own same-origin
    fetch — and header-less clients like curl / tests — working.
    """
    if not origin:
        return True
    try:
        return urlparse(origin).hostname in _LOCAL_HOSTS
    except ValueError:
        return False


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


def _r(v) -> float | None:
    """Round to 1 dp, or None for missing/non-numeric — for journal context."""
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


def _tf_context(symbol: str) -> dict:
    """Per-TF market context at order time (EMA side / ADX / DI / RSI), read
    DEFENSIVELY from the latest analysis so a missing field can never break an
    order. The front end renders a compact ↑/↓ tag from these raw figures."""
    out: dict = {}
    try:
        a = STATE.analysis
        sym = a.by_symbol.get(symbol) if a is not None else None
        for label, tf in (getattr(sym, "by_tf", {}) or {}).items():
            out[label] = {
                "ae": bool(getattr(tf, "above_ema", False)),
                "adx": _r(getattr(tf, "adx", None)),
                "dip": _r(getattr(tf, "di_plus", None)),
                "dim": _r(getattr(tf, "di_minus", None)),
                "rsi": _r(getattr(tf, "rsi", None)),
            }
    except Exception:  # noqa: BLE001 — context is best-effort, never fatal
        pass
    return out


def _journal_order(symbol: str, side: str, lots: float,
                   sl: float | None, tp: float | None, res: dict) -> None:
    """Append one journal entry for a just-placed order (broker-scoped)."""
    acct = STATE.account
    server = getattr(acct, "server", None) if acct is not None else None
    journal_store.append(server, {
        "ts": int(time.time() * 1000),
        "symbol": symbol, "side": side, "lots": lots,
        "sl": sl, "tp": tp,
        "ticket": res.get("order") or res.get("ticket"),
        "price": res.get("price"),
        "ctx": _tf_context(symbol),
    })


def build_app(connector=None) -> Flask:
    """Construct the Flask app, mount static + ``/ws``, return it.

    *connector* is the live :class:`MT5Connector`; when present the
    discretionary order endpoints (``/api/order``, ``/api/close``) are wired to
    it. ``None`` (e.g. the smoke verifier) leaves them returning 503.
    """
    app = Flask(
        __name__,
        static_folder=str(_STATIC_DIR),
        static_url_path="/static",
    )

    @app.after_request
    def _revalidate_assets(resp):
        # index.html + /static/* are patched live over the WebSocket, so force
        # the browser to REVALIDATE on every load (a cheap 304 when unchanged)
        # rather than serve a stale cached copy. This is what lets a plain
        # reload pick up a CSS/JS edit without needing a hard refresh.
        if request.path == "/" or request.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

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
        # Broker switch restarts the process — block forged cross-origin POSTs.
        if not _origin_is_local(request.headers.get("Origin")):
            return jsonify({"ok": False,
                            "error": "cross-origin request rejected"}), 403
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

    # ----------------------------------------------------- discretionary orders
    def _order_guard():
        """Shared gate for the order endpoints: master switch + same-origin."""
        if not config.TRADING_ENABLED:
            return jsonify({"ok": False, "error": "trading disabled (TRADING_ENABLED)"}), 403
        if not _origin_is_local(request.headers.get("Origin")):
            return jsonify({"ok": False, "error": "cross-origin request rejected"}), 403
        if connector is None:
            return jsonify({"ok": False, "error": "connector unavailable"}), 503
        return None

    @app.route("/api/order", methods=["POST"])
    def post_order():
        guard = _order_guard()
        if guard is not None:
            return guard
        body = request.get_json(silent=True) or {}
        symbol = body.get("symbol")
        side = body.get("side")
        if symbol not in {s.base for s in config.SYMBOLS}:
            return jsonify({"ok": False, "error": f"unknown symbol {symbol!r}"}), 400
        if side not in ("BUY", "SELL"):
            return jsonify({"ok": False, "error": f"bad side {side!r}"}), 400
        try:
            lots = float(body.get("lots"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "bad lots"}), 400
        if not (lots > 0):
            return jsonify({"ok": False, "error": "lots must be > 0"}), 400
        sl = body.get("sl") or None
        tp = body.get("tp") or None
        try:
            sl = float(sl) if sl is not None else None
            tp = float(tp) if tp is not None else None
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "bad sl/tp"}), 400
        res = connector.place_market_order(symbol, side, lots, sl=sl, tp=tp)
        log.info("order %s %s lots=%s sl=%s tp=%s -> %s", side, symbol, lots, sl, tp,
                 res.get("retcode", res.get("error")))
        if res.get("ok"):
            # Log the discretionary entry + the multi-TF context it was taken in.
            # Never let journalling affect the already-placed order.
            try:
                _journal_order(symbol, side, lots, sl, tp, res)
            except Exception:  # noqa: BLE001
                log.exception("journal append failed for %s (order placed)", symbol)
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.route("/api/journal", methods=["GET"])
    def get_journal():
        acct = STATE.account
        server = getattr(acct, "server", None) if acct is not None else None
        try:
            limit = min(500, max(1, int(request.args.get("limit", 200))))
        except (TypeError, ValueError):
            limit = 200
        return jsonify({"server": server,
                        "entries": journal_store.load_recent(server, limit)})

    @app.route("/api/close", methods=["POST"])
    def post_close():
        guard = _order_guard()
        if guard is not None:
            return guard
        body = request.get_json(silent=True) or {}
        if body.get("all"):
            res = connector.close_all(body.get("symbol") or None)
        else:
            try:
                ticket = int(body.get("ticket"))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "bad ticket"}), 400
            res = connector.close_position(ticket)
        log.info("close %s -> %s", body, res.get("ok"))
        return jsonify(res), (200 if res.get("ok") else 400)

    mount_websocket(app)
    log.info(
        "Lite dashboard built on http://%s:%d (static=%s, WS=%s)",
        config.DASH_HOST, config.DASH_PORT, _STATIC_DIR, config.DASH_WS_PATH,
    )
    return app
