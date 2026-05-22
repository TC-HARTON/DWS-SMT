"""End-to-end profile of the full analysis dispatcher.

Runs each handler N times against the real MT5 EXNESS connection (so the
numbers reflect what production sees) and prints a cumulative-time
breakdown plus the top hot functions. Use this to decide which path
needs Phase 5 attention vs. which is already well within budget.
"""

from __future__ import annotations

import cProfile
import io
import logging
import pstats
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(level=logging.WARNING)

import config
from analyzer.analysis_loop import AnalysisLoop
from analyzer.mt5_connector import MT5Connector


def main() -> int:
    connector = MT5Connector()
    loop = AnalysisLoop(connector)
    # We use the loop's _do_* handlers directly so the schedule timer noise
    # does not pollute the profile.
    loop._connector.initialize()
    loop._lines_watcher.start()
    loop._strength_engine.resolve_pairs()
    bases = list(loop._connector.resolved_symbols.keys())

    # Warm everything up — first cold-cache cycle is excluded from the profile.
    print("--- warmup ---")
    t0 = time.perf_counter()
    loop._run_analysis_pass(bases)
    loop._do_price_refresh(bases)
    # Calendar uses HTTP; let the daemon run once then complete.
    loop._do_calendar_refresh(bases)
    while loop._calendar_inflight.is_set():
        time.sleep(0.2)
    loop._do_heavy_refresh(bases)
    loop._do_history_refresh(bases)
    print(f"warmup took {(time.perf_counter()-t0)*1000:.1f} ms")

    pr = cProfile.Profile()
    pr.enable()

    print("\n--- profile: 5 cycles ---")
    cycle_ms: dict[str, list[float]] = {}
    for cycle in range(5):
        for name in ("price", "analysis", "heavy", "history"):
            handler = {
                "price": loop._do_price_refresh,
                "analysis": loop._do_analysis_refresh,
                "heavy": loop._do_heavy_refresh,
                "history": loop._do_history_refresh,
            }[name]
            t = time.perf_counter()
            handler(bases)
            ms = (time.perf_counter() - t) * 1000.0
            cycle_ms.setdefault(name, []).append(ms)

    pr.disable()
    loop._lines_watcher.stop()
    connector.shutdown()

    print()
    for name, samples in cycle_ms.items():
        mean = sum(samples) / len(samples)
        print(f"  {name:9s} mean={mean:7.2f}ms  min={min(samples):6.2f}  max={max(samples):6.2f}")

    print("\n--- top 25 cumulative ---")
    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(25)
    print(s.getvalue())

    print("--- top 15 internal (tottime) ---")
    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("tottime").print_stats(15)
    print(s.getvalue())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
