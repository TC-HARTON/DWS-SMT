"""Detailed cProfile of one full IndicatorEngine.compute() pass."""

from __future__ import annotations

import cProfile
import io
import pstats
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from analyzer.indicator_engine import IndicatorEngine
from analyzer.mt5_connector import MT5Connector


def main() -> int:
    with MT5Connector() as c:
        rates = c.fetch_rates_parallel([s.base for s in config.SYMBOLS], config.TIMEFRAMES)
        engine = IndicatorEngine()
        # Warm up.
        engine.compute(rates)
        engine.compute(rates)

        pr = cProfile.Profile()
        pr.enable()
        for _ in range(5):
            snap = engine.compute(rates)
        pr.disable()

    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(25)
    print(s.getvalue())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
