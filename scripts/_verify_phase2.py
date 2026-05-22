"""Verify Phase 2 wiring on a running dashboard.

* Connects to the WebSocket and inspects the first delivered snapshot for
  the new ``structures`` block.
* Writes a sample ``lines_XAUUSD.json`` into the EA output directory,
  then waits for the next snapshot and asserts the new line shows up.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import simple_websocket

import config

WS_URL = f"ws://{config.DASH_HOST}:{config.DASH_PORT}{config.DASH_WS_PATH}"


def _receive_snapshot(ws, timeout: float = 8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = ws.receive(timeout=1.0)
        if msg:
            return json.loads(msg)
    return None


def main() -> int:
    ws = simple_websocket.Client(WS_URL)
    try:
        snap = _receive_snapshot(ws, timeout=10.0)
        if snap is None:
            print("FAIL: no snapshot within 10 s")
            return 1
        version_before = snap["version"]
        structures = snap.get("structures") or {}
        levels_xau = (structures.get("levels_by_symbol") or {}).get("XAUUSD") or []
        pa_xau = (structures.get("price_action_by_symbol") or {}).get("XAUUSD") or []
        conf_xau = (structures.get("confluences_by_symbol") or {}).get("XAUUSD") or []
        print(f"version={version_before}")
        print(f"XAUUSD auto-detected levels: {len(levels_xau)}")
        print(f"XAUUSD price-action events: {len(pa_xau)}")
        print(f"XAUUSD confluence clusters: {len(conf_xau)}")
        kinds = {lv["kind"] for lv in levels_xau}
        print(f"kinds seen: {sorted(kinds)}")
        # Sanity: at least PDH/PDL, round, vwap should be present.
        required_minimum = {"pdh", "pdl", "round"}
        missing = required_minimum - kinds
        if missing:
            print(f"FAIL: expected at least {required_minimum}, missing {missing}")
            return 2
        print("OK: auto-detection populated structures")

        # ----- Now drop a sample lines_XAUUSD.json to test the EA path. -----
        lines_path = config.LINES_DIR / "lines_XAUUSD.json"
        payload = {
            "symbol": "XAUUSD",
            "updated_at": "2026-05-18T22:00:00+09:00",
            "lines": {
                "horizontal": [
                    {"name": "R1_strong_D1", "price": 9999.99, "color": "#FF0000"},
                ],
                "trendlines": [],
                "rectangles": [],
                "channels": [],
                "fibonacci": [],
                "texts": [],
            },
        }
        try:
            lines_path.parent.mkdir(parents=True, exist_ok=True)
            lines_path.write_text(json.dumps(payload), encoding="utf-8")
            print(f"wrote sample {lines_path.name}")
        except OSError as exc:
            print(f"FAIL: cannot write sample: {exc}")
            return 3

        # Watchdog should fire within a second; the next analysis cycle
        # (≤5 s later) folds the new EA level into the structures block.
        deadline = time.time() + 15.0
        ea_level = None
        while time.time() < deadline:
            snap = _receive_snapshot(ws, timeout=2.0)
            if snap is None:
                continue
            new_levels = ((snap.get("structures") or {}).get("levels_by_symbol")
                          or {}).get("XAUUSD") or []
            for lv in new_levels:
                if lv["source"] == "ea_user" and lv["name"] == "R1_strong_D1":
                    ea_level = lv
                    break
            if ea_level:
                break
        if ea_level is None:
            print("FAIL: EA-sourced level never appeared in snapshot")
            return 4
        print(f"OK: EA-sourced level appeared: {ea_level['name']} @ {ea_level['price']}")
        print(f"     importance={ea_level['importance']} tf={ea_level['tf']}")

        # Cleanup: remove the sample so it doesn't linger.
        try:
            lines_path.unlink()
            print(f"cleaned up {lines_path.name}")
        except OSError:
            pass
        print("ALL CHECKS PASSED")
        return 0
    finally:
        ws.close()


if __name__ == "__main__":
    raise SystemExit(main())
