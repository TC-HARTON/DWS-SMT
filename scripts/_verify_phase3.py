"""Verify Phase 3 wiring on a running dashboard."""

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
        # Phase 3 features publish on the 30s (strength/correlation) and 60s
        # (performance) cadences. Give the very first cycle up to 90 seconds.
        print("waiting for first strength snapshot…")
        snap = _wait_for(ws, lambda d: d.get("strength") is not None, timeout=90.0)
        if snap is None or not snap.get("strength"):
            print("FAIL: strength snapshot never arrived")
            return 1
        strength = snap["strength"]
        windows = strength["by_window"]
        print(f"strength windows: {sorted(windows)}")
        for label, w in windows.items():
            n_scores = len(w["scores"])
            print(f"  {label}: {n_scores} currencies, {len(w['pair_biases'])} pair biases")
        # Check H4 USD score exists.
        h4 = windows.get("H4")
        if h4 is None or "USD" not in h4["scores"]:
            print("FAIL: USD score missing in H4 window")
            return 2
        print(f"H4 USD score: {h4['scores']['USD']['score']:.2f}")

        print("waiting for correlation snapshot…")
        snap = _wait_for(ws, lambda d: d.get("correlation") is not None, timeout=30.0)
        if snap is None or not snap.get("correlation"):
            print("FAIL: correlation snapshot never arrived")
            return 3
        corr = snap["correlation"]
        windows = corr["by_window"]
        print(f"correlation windows: {sorted(windows)}")
        for bars, m in windows.items():
            print(f"  {bars} bars: {len(m['symbols'])}x{len(m['symbols'])} matrix")

        print("waiting for performance snapshot…")
        snap = _wait_for(ws, lambda d: d.get("performance") is not None, timeout=90.0)
        if snap is None or not snap.get("performance"):
            print("FAIL: performance snapshot never arrived")
            return 4
        perf = snap["performance"]
        ranges = list(perf["by_range"].keys())
        print(f"performance ranges: {ranges}")
        for label, r in perf["by_range"].items():
            print(f"  {label}: {r['trade_count']} trades, "
                  f"net={r['net_profit']}, win_rate={r['win_rate']}")
        print(f"open trades: {perf['open_trade_count']}")

        print("\nALL PHASE 3 CHECKS PASSED")
        return 0
    finally:
        ws.close()


if __name__ == "__main__":
    raise SystemExit(main())
