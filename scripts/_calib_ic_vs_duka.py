"""Feed-calibration: ICMarkets vs Dukascopy on the OVERLAP window.

Option A from the broker-history discussion: ICMarkets only retains bars back to
2023-03-20, while the deep (2010+) baseline is built on Dukascopy. To know how
far the live IC feed drifts from the Dukascopy reference, we run the IDENTICAL
production DWS-SMT engine (``dws_smt.compute_symbol`` + ``config.DWS_SMT_STACKS``)
on BOTH feeds over the same matched window and compare, per base TF.

Method (isolates FEED difference, not spread):
  * Both feeds cold-start at 2023-03-20 (IC has nothing earlier), end 2025-12-31
    (Dukascopy CSVs stop there). Same bars, same warmup, same rules.
  * A UNIFORM 2.0-pip cost is applied to BOTH (``config.LIVE_SPREAD_COST_PIPS``),
    so the spread column — unreliable in MT5 exports — never enters, and the only
    thing that differs between the two runs is the price bars themselves.
  * P/L in PIPS = trade.points / pip_price (point-size agnostic; the two feeds
    quote gold at different digits, which cancels here).

Run from project root::

    py scripts/_calib_ic_vs_duka.py [SYMBOL ...]      # default: XAUUSD
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from analyzer import dws_smt  # noqa: E402

# Market pip in price units (gold $0.10, JPY 0.01, FX 0.0001).
PIP_PRICE: dict[str, float] = {
    "XAUUSD": 0.10,
    "USDJPY": 0.01, "EURJPY": 0.01, "GBPJPY": 0.01, "AUDJPY": 0.01,
    "EURUSD": 0.0001, "GBPUSD": 0.0001, "AUDUSD": 0.0001,
}

# Dukascopy filename tokens (Bid/Ask, full-history date range in the name).
_DUKA_TFNAME = {"W1": "Weekly", "D1": "Daily", "H4": "4 Hours",
                "H1": "Hourly", "M15": "15 Mins"}
_DUKA_RANGE = {"W1": "2009.12.28_2025.12.29", "D1": "2010.01.01_2025.12.31",
               "H4": "2010.01.01_2025.12.31", "H1": "2010.01.01_2025.12.31",
               "M15": "2010.01.01_2025.12.31"}
# ICMarkets MT5-export filename token per TF (SYMBOL_<token>_<start>_<end>.csv).
_IC_TOKEN = {"W1": "Weekly", "D1": "Daily", "H4": "H4", "H1": "H1", "M15": "M15"}

# Matched comparison window (IC floor .. Dukascopy ceiling).
OVERLAP_START = pd.Timestamp("2023-03-20")
OVERLAP_END = pd.Timestamp("2025-12-31 23:59:59")

COST_PIPS = float(config.LIVE_SPREAD_COST_PIPS)   # uniform on both feeds
TFS = ("W1", "D1", "H4", "H1", "M15")
BASE_TFS = ("M15", "H1", "H4")


def _bucharest_to_utc(naive: pd.Series) -> pd.Series:
    """Localize broker/Dukascopy EET/EEST wall-clock to true UTC (DST-aware)."""
    return (naive.dt.tz_localize("Europe/Bucharest",
                                 ambiguous=True, nonexistent="shift_forward")
                 .dt.tz_convert("UTC").dt.tz_localize(None))


def _frame(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only the MT5-compatible OHLC columns compute_symbol needs."""
    df = df.copy()
    df["spread"] = 0
    df["real_volume"] = 0
    cols = ["open", "high", "low", "close", "tick_volume", "spread", "real_volume"]
    return df[cols].sort_index()


def load_duka(symbol: str, tf: str) -> pd.DataFrame:
    """Dukascopy Bid CSV (Bid drives triggers; Ask only used for spread, ignored)."""
    f = PROJECT_ROOT / f"{symbol}_{_DUKA_TFNAME[tf]}_Bid_{_DUKA_RANGE[tf]}.csv"
    df = pd.read_csv(f)
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={"Time (EET)": "time", "Open": "open", "High": "high",
                            "Low": "low", "Close": "close", "Volume": "tick_volume"})
    df["time"] = _bucharest_to_utc(pd.to_datetime(df["time"], format="%Y.%m.%d %H:%M:%S"))
    return _frame(df.set_index("time"))


def load_ic(symbol: str, tf: str) -> pd.DataFrame:
    """ICMarkets MT5 export (tab-sep; intraday has DATE+TIME, D1/W1 DATE only)."""
    matches = glob.glob(str(PROJECT_ROOT / f"{symbol}_{_IC_TOKEN[tf]}_2023*.csv"))
    if not matches:
        raise FileNotFoundError(f"{symbol}_{_IC_TOKEN[tf]}_2023*.csv")
    df = pd.read_csv(matches[0], sep="\t")
    df.columns = [c.strip("<>").lower() for c in df.columns]
    if "time" in df.columns:
        naive = pd.to_datetime(df["date"] + " " + df["time"], format="%Y.%m.%d %H:%M:%S")
    else:
        naive = pd.to_datetime(df["date"], format="%Y.%m.%d")
    df["t"] = _bucharest_to_utc(naive)
    df = df.rename(columns={"tickvol": "tick_volume"})
    return _frame(df.set_index("t"))


def frames_for(loader, symbol: str) -> dict[str, pd.DataFrame]:
    out = {}
    for tf in TFS:
        df = loader(symbol, tf)
        out[tf] = df[(df.index >= OVERLAP_START) & (df.index <= OVERLAP_END)]
    return out


def run_engine(frames: dict[str, pd.DataFrame]):
    emit = max(len(df) for df in frames.values()) + 100
    return dws_smt.compute_symbol(
        frames=frames, stacks=config.DWS_SMT_STACKS,
        period=config.DWS_SMT_PERIOD, smooth=config.DWS_SMT_SMOOTH, out_bars=emit)


def tf_stats(window, pip_price: float) -> dict | None:
    """Closed-trade stats in PIPS with a uniform cost. Captures entry epochs."""
    nets, entries = [], []
    tms = window.times_ms
    for t in window.trades:
        if t.is_open:
            continue
        nets.append(t.points / pip_price - COST_PIPS)
        entries.append(int(tms[t.entry_idx]))
    n = len(nets)
    if n == 0:
        return None
    wins = [x for x in nets if x > 0]
    los = [x for x in nets if x < 0]
    pf = (sum(wins) / -sum(los)) if los else float("inf")
    return {"n": n, "wr": len(wins) / n, "pf": pf,
            "exp": sum(nets) / n, "net": sum(nets), "entries": set(entries)}


def calibrate(symbol: str) -> None:
    pip = PIP_PRICE[symbol]
    print(f"\n{'='*72}\n{symbol}  feed calibration  (IC vs Dukascopy, "
          f"{OVERLAP_START.date()}..{OVERLAP_END.date()}, uniform {COST_PIPS}pip)\n{'='*72}")
    duka = run_engine(frames_for(load_duka, symbol))
    ic = run_engine(frames_for(load_ic, symbol))

    hdr = f"{'base':4} | {'feed':5} {'N':>5} {'WR':>6} {'PF':>6} {'期待値pips':>10} {'純pips':>11}"
    print(hdr + " | 共通entry / ドリフト")
    print("-" * len(hdr) + "-" * 24)
    for b in BASE_TFS:
        wd = duka.by_base.get(b)
        wi = ic.by_base.get(b)
        sd = tf_stats(wd, pip) if wd else None
        si = tf_stats(wi, pip) if wi else None
        if not sd or not si:
            print(f"{b:4} | (no trades on one feed)")
            continue
        shared = len(sd["entries"] & si["entries"])
        drift = (si["pf"] - sd["pf"]) / sd["pf"] * 100 if sd["pf"] else 0
        for name, s in (("Duka", sd), ("IC", si)):
            pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:6.2f}"
            print(f"{b:4} | {name:5} {s['n']:5d} {s['wr']*100:5.1f}% {pf} "
                  f"{s['exp']:+10.2f} {s['net']:+11.0f}", end="")
            if name == "Duka":
                print()
            else:
                jac = shared / max(1, len(sd["entries"] | si["entries"]))
                print(f" | 共通{shared} (一致{jac*100:.0f}%) / PF {drift:+.0f}%")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    symbols = sys.argv[1:] or ["XAUUSD"]
    for sym in symbols:
        calibrate(sym)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
