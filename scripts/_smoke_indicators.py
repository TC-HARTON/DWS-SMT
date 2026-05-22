"""Smoke test: pull real rates, compute indicators, print per-symbol summary."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")

import config
from analyzer.indicator_engine import IndicatorEngine
from analyzer.mt5_connector import MT5Connector


def main() -> int:
    with MT5Connector() as c:
        rates = c.fetch_rates_parallel([s.base for s in config.SYMBOLS], config.TIMEFRAMES)
        snap = IndicatorEngine().compute(rates)
        snap = IndicatorEngine.with_broker_names(snap, c.resolved_symbols)

    print(f"\ngenerated_at={snap.generated_at} compute_ms={snap.compute_ms:.2f}\n")
    for base, sym in snap.by_symbol.items():
        print(f"=== {base} ({sym.broker_name}) ===")
        for label in ("D1", "H4", "H1", "M15"):
            tf = sym.by_tf.get(label)
            if tf is None:
                print(f"  {label}: <no data>")
                continue
            ema_str = f"{tf.ema:.5f}" if tf.ema is not None else "  n/a "
            rsi_str = f"{tf.rsi:6.2f}" if tf.rsi is not None else "   n/a"
            atr_str = f"{tf.atr:.5f}" if tf.atr is not None else "  n/a "
            adx_str = f"{tf.adx:6.2f}" if tf.adx is not None else "   n/a"
            arrow = "▲" if tf.above_ema else ("▼" if tf.above_ema is False else "·")
            print(
                f"  {label}: close={tf.last_close:.5f} EMA{tf.ema_period}={ema_str}{arrow} "
                f"RSI={rsi_str} ATR={atr_str} ADX={adx_str}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
