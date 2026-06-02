"""One-time maintenance: REGENERATE the live trigger store at true UTC, undoing
the historical server-clock-offset corruption (the trigger-history duplication
bug) AND the cross-season DST smear in one pass.

Why regenerate rather than de-duplicate
---------------------------------------
The store accumulated copies of each trade under several bad whole-hour offsets
(stale-tick mis-detections) plus the correct one. Many trades have NO copy under
the correct offset, so collapsing existing rows cannot recover their true time.
The DWS trigger set is price-derived (offset-independent), so we recompute every
trade from current bars and stamp each entry at TRUE UTC.

True-UTC stamping
-----------------
Bar timestamps come straight from the connector's ``copy_rates``, which is now
DST-aware for servers in ``config.BROKER_TZ_BY_SERVER`` (IC Markets =
Europe/Bucharest): each bar gets the correct +2h (winter) / +3h (summer) offset.
Because regeneration and the live validation pass share that one code path, the
regenerated entry times match exactly what the running dashboard will compute —
so the live feed appends no shifted duplicates after this runs.

Run it once per broker after switching brokers (only the connected broker's
sub-directory is touched). The original file is preserved as ``<name>.bak``
before rewriting. ASCII-only output for the Windows cp932 console.

Run::

    "C:/.../python.exe" scripts/_regen_trigger_store.py            # apply
    "C:/.../python.exe" scripts/_regen_trigger_store.py --dry-run  # report only
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd  # noqa: F401  (kept for parity with other maintenance scripts)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config                                            # noqa: E402
from analyzer import dws_smt                             # noqa: E402
from analyzer import trigger_store                       # noqa: E402
from analyzer.mt5_connector import MT5Connector          # noqa: E402
from analyzer.signal_validator import SignalValidator    # noqa: E402


def _closed_rows(window, point: float, cost_pts: float) -> list[dict]:
    """Every CLOSED trade as a store row ``{t, d, p}`` (true-UTC entry ms).

    Net P/L mirrors ``signal_validator._recent_triggers_from_window``:
    ``points/point - cost_pts`` with the uniform round-trip cost
    (config.LIVE_SPREAD_COST_PIPS), NOT the unreliable per-bar spread field."""
    times_ms = window.times_ms
    p = point if point > 0.0 else 1.0
    rows: list[dict] = []
    for t in window.trades:
        if t.is_open:
            continue
        ei = t.entry_idx
        if not (0 <= ei < times_ms.size):
            continue
        net = round(t.points / p - cost_pts, 1)
        rows.append({"t": int(times_ms[ei]), "d": int(t.direction), "p": net})
    rows.sort(key=lambda r: r["t"])
    return rows


def main() -> int:
    dry_run = "--dry-run" in sys.argv[1:]

    conn = MT5Connector()
    conn.ensure_connected()
    acct = conn.account_snapshot()
    if acct is None or not acct.server:
        print("ERROR: no account/server from MT5; cannot locate the broker store.")
        return 2
    server = acct.server
    tz = config.BROKER_TZ_BY_SERVER.get(server)
    meta = conn.symbol_meta_dict()
    print(f"connected broker server: {server}")
    print(f"bar timezone: {tz or f'flat offset {conn.server_offset_sec() // 3600}h'}")
    print(f"mode: {'DRY-RUN (no writes)' if dry_run else 'APPLY (rewrites with .bak)'}")

    sv = SignalValidator(conn)
    for sym in (s.base for s in config.SYMBOLS):
        rates = conn.fetch_rates_parallel([sym], sv._deep_specs)
        # Strict filter on the current symbol: silently dropping the (base, tf)
        # key would let a future multi-symbol call mix DataFrames between
        # symbols (last-write-wins). Match signal_validator.compute's pattern.
        frames = {
            tf: rates[(sym, tf)]
            for spec in sv._deep_specs
            for tf in (spec.label,)
            if (sym, tf) in rates and not rates[(sym, tf)].empty
        }
        if not frames:
            continue
        res = dws_smt.compute_symbol(frames, out_bars=config.VALIDATION_HISTORY_BARS)
        if res is None:
            continue
        sym_meta = meta.get(sym, {})
        point = float(sym_meta.get("point", 1.0) or 1.0)
        pip_size = float(sym_meta.get("pip_size", point) or point)
        cost_pts = config.LIVE_SPREAD_COST_PIPS * pip_size / (point if point > 0 else 1.0)
        for base_tf, window in res.by_base.items():
            path = trigger_store.store_path(server, sym, base_tf)
            old_n = sum(1 for _ in path.open(encoding="utf-8")) if path.exists() else 0
            rows = _closed_rows(window, point, cost_pts)
            print(f"  {sym} {base_tf}: stored={old_n} -> regenerated={len(rows)}")
            if dry_run:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                bak = path.with_suffix(path.suffix + ".bak")
                if not bak.exists():                     # never clobber an existing backup
                    bak.write_bytes(path.read_bytes())
            with path.open("w", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(
                        {"t": int(r["t"]), "d": int(r["d"]), "p": round(float(r["p"]), 1)},
                        ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
