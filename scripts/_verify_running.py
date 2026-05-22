"""Probe a running dashboard: HTTP index + a single WebSocket snapshot.

Run main.py separately, then invoke this script. Exits 0 on success.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import simple_websocket  # transitive dep of flask-sock

import config

INDEX_URL = f"http://{config.DASH_HOST}:{config.DASH_PORT}/"
LAYOUT_URL = f"http://{config.DASH_HOST}:{config.DASH_PORT}/_dash-layout"
WS_URL = f"ws://{config.DASH_HOST}:{config.DASH_PORT}{config.DASH_WS_PATH}"


def probe_http() -> None:
    print(f"GET {INDEX_URL}")
    with urlopen(INDEX_URL, timeout=10) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        status = resp.status
    print(f"  index status={status} bytes={len(body)}")
    if status != 200:
        raise SystemExit(f"unexpected index status: {status}")
    # Dash 4.x ships the layout as JSON via /_dash-layout — that is what
    # actually contains our element IDs.
    print(f"GET {LAYOUT_URL}")
    with urlopen(LAYOUT_URL, timeout=10) as resp:
        layout_blob = resp.read().decode("utf-8", errors="replace")
        status = resp.status
    print(f"  layout status={status} bytes={len(layout_blob)}")
    must_contain = [
        '"app-root"',
        '"symbol-panel-XAUUSD"',
        '"symbol-panel-USDJPY"',
        '"account-card"',
        '"ws"',
    ]
    missing = [s for s in must_contain if s not in layout_blob]
    if missing:
        raise SystemExit(f"layout missing expected markers: {missing}")
    print("  layout OK (every expected component id present)")


def probe_websocket() -> None:
    print(f"WS  {WS_URL}")
    ws = simple_websocket.Client(WS_URL)
    try:
        # Wait up to 5 s for the initial snapshot.
        deadline = time.time() + 5.0
        first = None
        while time.time() < deadline:
            msg = ws.receive(timeout=1.0)
            if msg is not None:
                first = msg
                break
        if first is None:
            raise SystemExit("WS no message received within 5 s")
        data = json.loads(first)
        print(f"  initial snapshot version={data.get('version')} "
              f"price_keys={list((data.get('price') or {}).get('ticks', {}).keys())[:3]}... "
              f"analysis_syms={len((data.get('analysis') or {}).get('by_symbol') or {})} "
              f"account_login={(data.get('account') or {}).get('login')}")
        # Wait briefly for a delta (price 1 s refresh should fire one).
        delta = ws.receive(timeout=3.0)
        if delta:
            delta_data = json.loads(delta)
            if delta_data["version"] > data["version"]:
                print(f"  delta received: version {data['version']} → {delta_data['version']}")
            else:
                print(f"  delta has same/lower version ({delta_data['version']}) — heartbeat")
        else:
            print("  no delta within 3 s (might be acceptable when market closed)")
    finally:
        ws.close()


def main() -> int:
    probe_http()
    probe_websocket()
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
