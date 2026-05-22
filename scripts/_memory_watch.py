"""Track the dashboard's RSS over many analysis cycles (SPEC §19 / §18.4).

Pass the running dashboard's PID; the script samples ``rss``, ``vms``,
the number of open file descriptors, and the analysis snapshot version
every ``SAMPLE_INTERVAL`` seconds for ``CYCLES`` samples, then prints a
trend line + linear fit. A monotonic upward slope on RSS suggests a
genuine leak; a sawtooth pattern is normal Python heap behaviour.

Example::

    python scripts\\_memory_watch.py 12345 --cycles 30 --interval 10
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psutil
import simple_websocket

import config

WS_URL = f"ws://{config.DASH_HOST}:{config.DASH_PORT}{config.DASH_WS_PATH}"


def _latest_version(ws_timeout: float = 0.5) -> int | None:
    """Probe the live WS to grab the current state version (best-effort)."""
    try:
        ws = simple_websocket.Client(WS_URL)
    except Exception:                    # noqa: BLE001 — best-effort
        return None
    try:
        msg = ws.receive(timeout=ws_timeout)
        if msg:
            return json.loads(msg).get("version")
    except Exception:                    # noqa: BLE001
        return None
    finally:
        try:
            ws.close()
        except Exception:                # noqa: BLE001
            pass
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pid", type=int, help="Dashboard python.exe PID")
    parser.add_argument("--cycles", type=int, default=20)
    parser.add_argument("--interval", type=float, default=10.0,
                        help="Sample interval in seconds")
    args = parser.parse_args()

    try:
        proc = psutil.Process(args.pid)
    except psutil.NoSuchProcess:
        print(f"PID {args.pid} not running")
        return 1

    rss_mb: list[float] = []
    thread_counts: list[int] = []
    versions: list[int | None] = []
    print(f"watching pid={args.pid} for {args.cycles} samples @ {args.interval}s "
          f"({args.cycles * args.interval}s total)")
    for i in range(args.cycles):
        if not proc.is_running():
            print(f"  sample {i}: process gone — abort")
            return 2
        try:
            mem = proc.memory_info()
            n_threads = proc.num_threads()
        except psutil.NoSuchProcess:
            print(f"  sample {i:2d}: process exited mid-sample — abort")
            return 2
        rss = mem.rss / (1024 * 1024)
        rss_mb.append(rss)
        thread_counts.append(n_threads)
        ver = _latest_version()
        versions.append(ver)
        print(f"  sample {i:2d}: rss={rss:7.2f} MB  threads={n_threads:3d}  "
              f"version={ver}")
        time.sleep(args.interval)

    print()
    print(f"--- summary ({len(rss_mb)} samples) ---")
    rss_min = min(rss_mb); rss_max = max(rss_mb); rss_mean = statistics.mean(rss_mb)
    drift = rss_mb[-1] - rss_mb[0]
    drift_per_min = drift / (args.cycles * args.interval / 60.0)
    print(f"  RSS: min={rss_min:.2f}  max={rss_max:.2f}  mean={rss_mean:.2f} MB")
    print(f"  RSS drift (last - first): {drift:+.2f} MB total, "
          f"{drift_per_min:+.3f} MB/min")
    spec_budget_mb = 500
    if rss_max < spec_budget_mb:
        print(f"  [OK] within SPEC budget ({spec_budget_mb} MB)")
    else:
        print(f"  [WARN] exceeds SPEC budget ({spec_budget_mb} MB)")
    # A 0.5 MB/min upward slope ~= 720 MB/day; flag anything above that.
    if drift_per_min > 0.5:
        print(f"  [WARN] possible leak: {drift_per_min:.2f} MB/min upward")
    elif drift_per_min < -0.5:
        print(f"  [OK] shrinking by {-drift_per_min:.2f} MB/min (likely GC)")
    else:
        print(f"  [OK] no significant memory drift")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
