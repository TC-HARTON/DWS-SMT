"""Macro / rate-differential layer (precision-optimization spec, Section B).

Fetches each tracked currency's central-bank policy rate (USD/EUR/GBP/JPY/AUD)
plus US employment, then derives a per-currency-pair rate differential and a
macro direction. The dashboard uses this to (a) show a reference panel and
(b) flag DWS-SMT triggers that fight the carry.

Honest scope note: policy rates give the structural *carry* direction plus
actual hike/cut events — not the market-implied rate-expectation momentum
(which needs OIS / rate futures, out of scope). The macro direction here is a
carry-alignment signal, useful for catching counter-carry trades, not a
substitute for rate-expectation analysis.

Every fetch is plain HTTP — this module never touches the MT5 connector.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime

import config

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class MacroRate:
    """One currency's central-bank policy rate."""

    currency: str
    rate: float
    as_of: str
    prev_rate: float | None
    source: str
    stale: bool


@dataclass(frozen=True)
class MacroEmployment:
    """Latest US employment readings (NFP change + unemployment rate)."""

    nonfarm_change: float | None
    unemployment_rate: float | None
    as_of: str
    prev_nonfarm_change: float | None
    source: str


@dataclass(frozen=True)
class MacroPairBias:
    """Rate differential + macro direction for one currency pair."""

    pair: str
    base_ccy: str
    quote_ccy: str
    differential: float
    macro_dir: int
    label: str


@dataclass(frozen=True)
class MacroSnapshot:
    """One macro refresh: rates, employment, and per-pair bias."""

    generated_at: float
    fetched_at: float
    rates: dict[str, MacroRate]
    employment: MacroEmployment | None
    by_pair: dict[str, MacroPairBias]
    last_error: str | None
    consecutive_failures: int


# --------------------------------------------------------------------------- #
# Per-pair bias
# --------------------------------------------------------------------------- #

def _split_pair(pair: str) -> tuple[str, str]:
    """Split a 6/7-char symbol into (base ccy, quote ccy).

    ``"USDJPY" -> ("USD", "JPY")``; ``"XAUUSD" -> ("XAU", "USD")``.
    """
    return pair[:3], pair[3:]


def pair_macro_bias(pair: str, rates: dict[str, MacroRate]) -> MacroPairBias:
    """Compute the rate differential and macro direction for *pair*.

    For a fiat/fiat pair: ``differential = rate(base) - rate(quote)`` and
    ``macro_dir`` is its sign — the high-yield currency has structural carry
    support. ``XAUUSD`` is special: gold carries no yield, so the macro driver
    is the US rate *trend* — a rising US rate is a headwind for gold
    (``macro_dir = -1``), a falling rate a tailwind (``+1``).

    If either leg's rate is missing or ``stale``, ``macro_dir`` is ``0`` — the
    filter must never penalise a trigger on bad/absent data.
    """
    base_ccy, quote_ccy = _split_pair(pair)

    if base_ccy == "XAU":
        usd = rates.get("USD")
        if usd is None or usd.stale or usd.prev_rate is None:
            return MacroPairBias(pair, base_ccy, quote_ccy, 0.0, 0, "—")
        delta = usd.rate - usd.prev_rate
        macro_dir = -1 if delta > 0 else (1 if delta < 0 else 0)
        label = ("米金利上昇=金に逆風" if macro_dir < 0
                 else "米金利低下=金に追風" if macro_dir > 0 else "—")
        return MacroPairBias(pair, base_ccy, quote_ccy, delta, macro_dir, label)

    base = rates.get(base_ccy)
    quote = rates.get(quote_ccy)
    if base is None or quote is None or base.stale or quote.stale:
        return MacroPairBias(pair, base_ccy, quote_ccy, 0.0, 0, "—")

    differential = base.rate - quote.rate
    macro_dir = 1 if differential > 0 else (-1 if differential < 0 else 0)
    if macro_dir > 0:
        label = f"{base_ccy}金利優位"
    elif macro_dir < 0:
        label = f"{quote_ccy}金利優位"
    else:
        label = "金利差なし"
    return MacroPairBias(pair, base_ccy, quote_ccy, differential, macro_dir, label)


# --------------------------------------------------------------------------- #
# Source parsers — pure functions, each returns (ISO date, rate)
# --------------------------------------------------------------------------- #

def parse_fred_json(body: str) -> tuple[str, float]:
    """Parse a FRED ``series/observations`` JSON body → (latest date, value).

    FRED encodes a missing observation as ``"."`` — those rows are skipped so
    the latest *real* value is returned.
    """
    doc = json.loads(body)
    obs = doc.get("observations") or []
    for row in reversed(obs):
        raw = (row.get("value") or "").strip()
        if raw and raw != ".":
            return str(row.get("date") or "")[:10], float(raw)
    raise ValueError("FRED response had no usable observation")


def parse_ecb_csv(body: str) -> tuple[str, float]:
    """Parse an ECB SDMX ``csvdata`` body → (latest date, rate).

    Columns are addressed by header name (``TIME_PERIOD`` / ``OBS_VALUE``) so a
    column-order change upstream does not break the parser. The last data row
    is the most recent observation.
    """
    reader = csv.DictReader(io.StringIO(body.strip()))
    rows = [r for r in reader if (r.get("OBS_VALUE") or "").strip()]
    if not rows:
        raise ValueError("ECB response had no OBS_VALUE rows")
    last = rows[-1]
    return str(last["TIME_PERIOD"])[:10], float(last["OBS_VALUE"])


def parse_boe_csv(body: str) -> tuple[str, float]:
    """Parse the BoE IADB CSV (series IUDBEDR) → (latest date ISO, rate).

    The IADB CSV is ``DATE,IUDBEDR`` with dates like ``02 Jan 2020``. The last
    non-empty data row is the most recent Bank Rate.
    """
    last_date = ""
    last_rate: float | None = None
    for row in csv.reader(io.StringIO(body)):
        if len(row) < 2:
            continue
        d, v = row[0].strip(), row[1].strip()
        if not v or d.upper() in {"DATE", "SERIES"}:
            continue
        try:
            rate = float(v)
        except ValueError:
            continue
        try:
            iso = datetime.strptime(d, "%d %b %Y").strftime("%Y-%m-%d")
        except ValueError:
            iso = d
        last_date, last_rate = iso, rate
    if last_rate is None:
        raise ValueError("BoE CSV had no usable IUDBEDR row")
    return last_date, last_rate


def parse_boj_html(body: str) -> tuple[str, float]:
    """Parse the BoJ ``fm01_d_1_en.html`` page → (latest date ISO, call rate).

    The page embeds table rows ``<th>YYYY/MM/DD</th><td> value</td>`` for the
    uncollateralised overnight call rate (the BoJ policy-rate proxy). The most
    recent non-``NA`` row is returned. Parsed with a regex so no HTML library
    is needed.
    """
    pattern = re.compile(
        r"(\d{4})/(\d{2})/(\d{2})\s*</[^>]+>\s*<[^>]+>\s*"
        r"(-?\d+(?:\.\d+)?|NA)",
        re.IGNORECASE,
    )
    last_date = ""
    last_rate: float | None = None
    for m in pattern.finditer(body):
        yyyy, mm, dd, val = m.groups()
        if val.upper() == "NA":
            continue
        last_date = f"{yyyy}-{mm}-{dd}"
        last_rate = float(val)
    if last_rate is None:
        raise ValueError("BoJ page had no usable call-rate row")
    return last_date, last_rate
