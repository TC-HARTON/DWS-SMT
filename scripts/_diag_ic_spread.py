"""Live IC Markets spread diagnostic — compares the MT5 terminal's native
spread (POINTS) against what the dashboard renders (pips via pip_size), so we
can confirm/refute a 10x display discrepancy and pin its exact cause.

Read-only: initialises the IC Markets terminal, pulls symbol_info +
symbol_info_tick for every configured symbol, and prints, per symbol:

    digits, point, MT5 spread(points), raw (ask-bid),
    pip_size (= point*10 if odd digits else point)  -- the dashboard rule,
    dashboard display (= (ask-bid)/pip_size),
    MT5-terminal points (= (ask-bid)/point),
    ratio terminal/dashboard.

Run:  py scripts/_diag_ic_spread.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import MetaTrader5 as mt5      # noqa: E402
import config                  # noqa: E402
from analyzer.mt5_connector import pip_size_for   # noqa: E402  (the real rule)

IC_PATH = config.BROKER_PRESETS["IC Markets"]
BASES = [s.base for s in config.SYMBOLS]


def _resolve(base: str) -> str | None:
    """Find the broker symbol for *base* (handles IC suffixes like .r/.a)."""
    if mt5.symbol_info(base) is not None:
        return base
    allsyms = mt5.symbols_get()
    if not allsyms:
        return None
    cands = [s.name for s in allsyms if s.name.upper().startswith(base.upper())]
    # Prefer the shortest match (plain base before suffixed variants).
    cands.sort(key=len)
    return cands[0] if cands else None


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if not mt5.initialize(path=IC_PATH):
        print(f"initialize FAILED for IC path: {IC_PATH}")
        print(f"  last_error={mt5.last_error()}")
        return 1

    term = mt5.terminal_info()
    acct = mt5.account_info()
    print("=" * 100)
    print(f" IC Markets MT5  |  terminal={getattr(term, 'name', '?')}  "
          f"company={getattr(term, 'company', '?')}")
    if acct is not None:
        print(f" account login={acct.login}  server={acct.server}  "
              f"currency={acct.currency}  leverage=1:{acct.leverage}")
    print("=" * 100)
    hdr = (f"{'symbol':9}{'dig':>4}{'point':>8}{'bid':>11}{'ask':>11}"
           f"{'ask-bid':>10}{'pip_size':>9}{'DASH(pip)':>10}"
           f"{'TERM(pt)':>9}{'ratio':>6}")
    print(hdr)
    print("-" * 100)

    for base in BASES:
        broker = _resolve(base)
        if broker is None:
            print(f"{base:10}  (not found in Market Watch)")
            continue
        mt5.symbol_select(broker, True)
        info = mt5.symbol_info(broker)
        tick = mt5.symbol_info_tick(broker)
        if info is None or tick is None:
            print(f"{base:10}  (no info/tick)")
            continue
        digits = int(info.digits)
        point = float(info.point)
        mt5_sp_pts = int(info.spread)                 # MT5 native spread, points
        raw = float(tick.ask) - float(tick.bid)
        pip_size = pip_size_for(base, digits, point)  # the REAL dashboard rule
        dash = raw / pip_size if pip_size > 0 else 0.0       # dashboard renders this
        term_pts = raw / point if point > 0 else 0.0         # MT5 terminal column
        ratio = (term_pts / dash) if dash > 0 else float("nan")
        print(f"{base:9}{digits:>4}{point:>8.5f}{float(tick.bid):>11.{digits}f}"
              f"{float(tick.ask):>11.{digits}f}{raw:>10.5f}{pip_size:>9.5f}"
              f"{dash:>10.2f}{term_pts:>9.1f}{ratio:>6.2f}")

    print("-" * 100)
    print(" DASH(pip) = what the dashboard shows = (ask-bid)/pip_size")
    print(" TERM(pt)  = what the MT5 terminal Spread column shows = (ask-bid)/point")
    print(" ratio>1   => terminal value is 'ratio'x the dashboard (odd digits => 10x)")
    mt5.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
