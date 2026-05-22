"""Connect to the running dashboard and record compute_ms over 30 s."""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import simple_websocket

import config

WS_URL = f"ws://{config.DASH_HOST}:{config.DASH_PORT}{config.DASH_WS_PATH}"


def main() -> int:
    ws = simple_websocket.Client(WS_URL)
    samples: list[float] = []
    deadline = time.time() + 30.0
    seen = set()
    try:
        while time.time() < deadline:
            msg = ws.receive(timeout=2.0)
            if not msg:
                continue
            blob = json.loads(msg)
            a = blob.get("analysis")
            if a and a.get("compute_ms") is not None:
                ts = a.get("generated_at")
                if ts not in seen:
                    seen.add(ts)
                    samples.append(float(a["compute_ms"]))
    finally:
        ws.close()
    if not samples:
        print("no analysis samples received")
        return 1
    print(f"n={len(samples)}  mean={statistics.mean(samples):.2f} ms  "
          f"min={min(samples):.2f}  max={max(samples):.2f}  "
          f"p95={sorted(samples)[int(len(samples)*0.95)]:.2f}")
    print(f"under 50ms budget: {sum(1 for s in samples if s < 50)} / {len(samples)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
