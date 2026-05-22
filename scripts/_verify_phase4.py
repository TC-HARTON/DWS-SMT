"""Verify Phase 4 wiring on a running dashboard."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import simple_websocket

import config

WS_URL = f"ws://{config.DASH_HOST}:{config.DASH_PORT}{config.DASH_WS_PATH}"


def _wait_for(ws, predicate, *, timeout: float):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        msg = ws.receive(timeout=1.0)
        if not msg:
            continue
        last = json.loads(msg)
        if predicate(last):
            return last
    return last


def main() -> int:
    print(f"connecting to {WS_URL}")
    ws = simple_websocket.Client(WS_URL)
    try:
        print("waiting for first calendar snapshot…")
        # The very first calendar cycle runs as soon as the loop starts (or
        # within 1 s); give it 60 s to absorb HTTP timeout / retries.
        snap = _wait_for(ws, lambda d: d.get("calendar") is not None, timeout=60.0)
        if snap is None or not snap.get("calendar"):
            print("FAIL: calendar snapshot never arrived")
            return 1
        cal = snap["calendar"]
        print(f"source: {cal['source']}")
        print(f"events: {len(cal['events'])}")
        if cal['last_error']:
            print(f"last_error: {cal['last_error']}")
        for ev in cal["events"][:5]:
            print(f"  {ev['currency']} @ {ev['release_ts']:.0f}: "
                  f"{ev['title']} (forecast={ev['forecast']}, prev={ev['previous']})")
        if cal['source'] not in {"forex_factory", "mt5", "stale_cache"}:
            print(f"FAIL: unexpected source {cal['source']}")
            return 2
        print("\nALL PHASE 4 CHECKS PASSED")
        return 0
    finally:
        ws.close()


if __name__ == "__main__":
    raise SystemExit(main())
