"""Compare what the dashboard sends vs what MT5 reports for the same instant."""
from __future__ import annotations
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import MetaTrader5 as mt5
import simple_websocket
import config

WS_URL = f"ws://{config.DASH_HOST}:{config.DASH_PORT}{config.DASH_WS_PATH}"

mt5.initialize(path=config.MT5_TERMINAL_PATH)
ws = simple_websocket.Client(WS_URL)
# Read latest snapshot
snap = None
for _ in range(5):
    msg = ws.receive(timeout=1.0)
    if msg:
        snap = json.loads(msg)
        if snap.get("price"):
            break
ws.close()

ticks_ws = (snap.get("price") or {}).get("ticks") or {}
ws_ts = snap.get("ts")

print(f"WS snapshot ts={ws_ts}")
print(f"{'symbol':8s} {'WS bid':>12s} {'WS ask':>12s} {'MT5 bid':>12s} {'MT5 ask':>12s} "
      f"{'Δbid':>10s} {'WS ageS':>8s}")
for base in [s.base for s in config.SYMBOLS]:
    ws_t = ticks_ws.get(base) or {}
    mt5_t = mt5.symbol_info_tick(base)
    if mt5_t is None:
        print(f"{base:8s} ws_bid={ws_t.get('bid')} mt5: no tick")
        continue
    ws_bid = ws_t.get('bid'); ws_ask = ws_t.get('ask')
    mt5_bid = mt5_t.bid; mt5_ask = mt5_t.ask
    ws_age = (ws_ts - (ws_t.get('time_msc') or 0) / 1000) if ws_t.get('time_msc') else None
    dbid = (ws_bid - mt5_bid) if (ws_bid is not None and mt5_bid) else None
    print(f"{base:8s} {ws_bid!s:>12s} {ws_ask!s:>12s} {mt5_bid:12.5f} {mt5_ask:12.5f} "
          f"{(dbid if dbid is not None else 0):>10.5f} {(ws_age or 0):>8.2f}")
mt5.shutdown()
