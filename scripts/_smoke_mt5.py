"""Quick smoke check for analyzer.mt5_connector — not part of the test suite."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")

import config
from analyzer.mt5_connector import MT5Connector


def main() -> int:
    with MT5Connector() as c:
        print("resolved:", c.resolved_symbols)
        t0 = time.perf_counter()
        ticks = c.latest_ticks()
        dt_ticks = (time.perf_counter() - t0) * 1000
        print(f"latest_ticks ({len(ticks)} symbols) took {dt_ticks:.1f} ms")
        for base, tick in ticks.items():
            print(f"  {base:7s} -> bid={tick.bid:.5f} ask={tick.ask:.5f}")

        t0 = time.perf_counter()
        rates = c.fetch_rates_parallel(
            [s.base for s in config.SYMBOLS],
            config.TIMEFRAMES,
        )
        dt_rates = (time.perf_counter() - t0) * 1000
        print(f"fetch_rates_parallel ({len(rates)} (sym,tf)) took {dt_rates:.1f} ms")

        acc = c.account_snapshot()
        if acc:
            print(
                f"account: login={acc.login} balance={acc.balance:.2f} {acc.currency} "
                f"equity={acc.equity:.2f} positions={len(acc.positions)}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
