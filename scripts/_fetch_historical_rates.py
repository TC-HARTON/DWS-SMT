"""One-shot fetch of historical policy rates from FRED for the 5 currencies
the dashboard tracks. Caches to data/historical_rates.csv so the swap-cost
modelling in _backtest_all_sl_wf.py can run offline.

Series IDs use the OECD-harmonised "Immediate rates ≤ 24 h" family, which
is the closest universally-available proxy for each central bank's
official policy rate:

  USD  IRSTCI01USM156N
  EUR  IRSTCI01EZM156N
  GBP  IRSTCI01GBM156N
  JPY  IRSTCI01JPM156N
  AUD  IRSTCI01AUM156N

Monthly resolution. Policy rates change at most ~10×/year per central bank,
so monthly is fine for backtest swap accumulation (we forward-fill between
observations).
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Read FRED API key from .env (the dashboard already uses this convention).
ENV_FILE = PROJECT_ROOT / ".env"
api_key = ""
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("FRED_API_KEY="):
            api_key = line.split("=", 1)[1].strip()
            break
if not api_key:
    print("ERROR: FRED_API_KEY missing in .env", file=sys.stderr)
    sys.exit(1)


FRED_SERIES = {
    "USD": "IRSTCI01USM156N",
    "EUR": "IRSTCI01EZM156N",
    "GBP": "IRSTCI01GBM156N",
    "JPY": "IRSTCI01JPM156N",
    "AUD": "IRSTCI01AUM156N",
}

OUT_FILE = PROJECT_ROOT / "data" / "historical_rates.csv"


def fetch_series(series_id: str) -> pd.DataFrame:
    url = ("https://api.stlouisfed.org/fred/series/observations"
           f"?series_id={series_id}"
           f"&api_key={api_key}"
           "&observation_start=2009-01-01"
           "&observation_end=2025-12-31"
           "&file_type=json")
    with urllib.request.urlopen(url, timeout=20) as r:
        body = json.loads(r.read().decode("utf-8"))
    rows = []
    for obs in body.get("observations", []):
        v = obs.get("value", ".")
        if v == ".":
            continue
        rows.append({"date": obs["date"], "rate": float(v)})
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date").sort_index()


def main() -> int:
    print("Fetching historical policy rates from FRED…")
    frames = {}
    for ccy, sid in FRED_SERIES.items():
        try:
            df = fetch_series(sid)
        except (urllib.error.URLError, json.JSONDecodeError, KeyError, ValueError) as e:
            print(f"  {ccy}: FAILED ({e})", file=sys.stderr)
            return 1
        frames[ccy] = df
        print(f"  {ccy:3s}  {sid:18s}  N={len(df):4d}  "
              f"range {df.index.min().date()} → {df.index.max().date()}  "
              f"latest {df['rate'].iloc[-1]:.3f}%")

    # Merge into one wide DataFrame, daily index, forward-filled.
    daily = pd.date_range("2009-01-01", "2025-12-31", freq="D")
    out = pd.DataFrame(index=daily)
    out.index.name = "date"
    for ccy, df in frames.items():
        out[ccy] = df["rate"].reindex(daily, method="ffill")

    # Some series don't start exactly on 2009-01-01 — drop leading NaN rows.
    out = out.dropna(how="any")
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT_FILE)
    print(f"\nSaved {len(out):,d} daily rows × {len(out.columns)} currencies → {OUT_FILE}")
    print(f"  Spans {out.index.min().date()} → {out.index.max().date()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
