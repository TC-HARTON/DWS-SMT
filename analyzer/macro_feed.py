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
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests

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

    Returns the observation with the most recent date, independent of the
    response's sort order. FRED encodes a missing observation as ``"."`` —
    those rows are skipped.
    """
    doc = json.loads(body)
    usable: list[tuple[str, float]] = []
    for row in doc.get("observations") or []:
        date = str(row.get("date") or "")[:10]
        raw = (row.get("value") or "").strip()
        if date and raw and raw != ".":
            usable.append((date, float(raw)))
    if not usable:
        raise ValueError("FRED response had no usable observation")
    return max(usable, key=lambda t: t[0])     # ISO dates sort lexically


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


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class MacroEngine:
    """Fetches every macro source, caches to disk, builds a MacroSnapshot.

    Per-source isolation: a single central bank failing marks only that
    currency ``stale`` (value re-used from cache when available); the rest of
    the snapshot is unaffected. Mirrors :class:`analyzer.calendar_feed.CalendarEngine`.

    USD and AUD both come from FRED — the Fed funds target and the RBA cash
    rate (the RBA's own site blocks automated requests). EUR/GBP/JPY come from
    the ECB / BoE / BoJ directly. Every fetch is plain HTTP; this engine never
    touches the MT5 connector.
    """

    def __init__(
        self,
        cache_file: Path = config.MACRO_CACHE_FILE,
        timeout: float = config.MACRO_FETCH_TIMEOUT_SEC,
    ) -> None:
        self._cache_file = Path(cache_file)
        self._timeout = timeout
        self._consecutive_failures = 0
        self._last_error: str | None = None
        self._last_fetch_ok = 0.0
        self._cached_rates: dict[str, MacroRate] = {}
        self._bootstrap_from_cache()

    # ----------------------------------------------------------- compute
    def compute(self) -> MacroSnapshot:
        """One refresh cycle: fetch every source, build the snapshot."""
        rates: dict[str, MacroRate] = {}
        errors: list[str] = []
        for ccy in config.MACRO_CURRENCIES:
            try:
                rates[ccy] = self._fetch_rate(ccy)
            except (requests.RequestException, ValueError, KeyError) as exc:
                errors.append(f"{ccy}: {exc}")
                cached = self._cached_rates.get(ccy)
                if cached is not None:
                    rates[ccy] = MacroRate(
                        cached.currency, cached.rate, cached.as_of,
                        cached.prev_rate, cached.source, stale=True)

        try:
            employment = self._fetch_employment()
        except (requests.RequestException, ValueError, KeyError) as exc:
            errors.append(f"employment: {exc}")
            employment = None

        if errors:
            self._consecutive_failures += 1
            self._last_error = "; ".join(errors)
            log.warning("macro: %d source error(s) - %s",
                        len(errors), self._last_error)
        else:
            self._consecutive_failures = 0
            self._last_error = None
            self._last_fetch_ok = time.time()

        if rates:
            self._cached_rates = dict(rates)
            self._store_cache(rates)

        by_pair = {
            s.base: pair_macro_bias(s.base, rates) for s in config.SYMBOLS
        }
        return MacroSnapshot(
            generated_at=time.time(),
            fetched_at=self._last_fetch_ok,
            rates=rates,
            employment=employment,
            by_pair=by_pair,
            last_error=self._last_error,
            consecutive_failures=self._consecutive_failures,
        )

    # ------------------------------------------------------- per-source
    def _fetch_rate(self, ccy: str) -> MacroRate:
        """Fetch one currency's policy rate from its central-bank source."""
        if ccy == "USD":
            as_of, rate = parse_fred_json(
                self._fred_get(config.MACRO_FRED_RATE_SERIES))
            source = "fred"
        elif ccy == "AUD":
            as_of, rate = parse_fred_json(
                self._fred_get(config.MACRO_FRED_AUD_SERIES))
            source = "fred"
        elif ccy == "EUR":
            as_of, rate = parse_ecb_csv(self._http_get(config.MACRO_ECB_URL))
            source = "ecb"
        elif ccy == "GBP":
            as_of, rate = parse_boe_csv(self._http_get(config.MACRO_BOE_URL))
            source = "boe"
        elif ccy == "JPY":
            as_of, rate = parse_boj_html(self._http_get(config.MACRO_BOJ_URL))
            source = "boj"
        else:
            raise ValueError(f"no macro source for {ccy}")
        prev = self._cached_rates.get(ccy)
        if prev is not None and prev.rate != rate:
            prev_rate: float | None = prev.rate
        elif prev is not None:
            prev_rate = prev.prev_rate
        else:
            prev_rate = None
        return MacroRate(ccy, rate, as_of, prev_rate, source, stale=False)

    def _fetch_employment(self) -> MacroEmployment | None:
        """Fetch US nonfarm-payroll change + unemployment rate from FRED."""
        as_of, nfp_chg, prev_chg = self._fred_payems_change()
        _, unrate = parse_fred_json(
            self._fred_get(config.MACRO_FRED_UNRATE_SERIES))
        return MacroEmployment(
            nonfarm_change=nfp_chg,
            unemployment_rate=unrate,
            as_of=as_of,
            prev_nonfarm_change=prev_chg,
            source="fred",
        )

    def _fred_payems_change(self) -> tuple[str, float | None, float | None]:
        """Latest + previous month-over-month change in PAYEMS (thousands)."""
        doc = json.loads(self._fred_get(config.MACRO_FRED_PAYEMS_SERIES, limit=4))
        vals = [(o["date"][:10], float(o["value"]))
                for o in doc.get("observations", [])
                if (o.get("value") or "").strip() not in ("", ".")]
        vals.sort(key=lambda t: t[0])          # oldest → newest, regardless of fetch order
        if len(vals) < 2:
            return (vals[-1][0] if vals else ""), None, None
        latest = vals[-1][1] - vals[-2][1]
        prev = vals[-2][1] - vals[-3][1] if len(vals) >= 3 else None
        return vals[-1][0], latest, prev

    # -------------------------------------------------------------- HTTP
    def _http_get(self, url: str) -> str:
        """HTTP GET with the browser User-Agent (BoE rejects bot UAs)."""
        resp = requests.get(
            url, timeout=self._timeout,
            headers={"User-Agent": config.MACRO_HTTP_USER_AGENT},
        )
        resp.raise_for_status()
        return resp.text

    def _fred_get(self, series_id: str, limit: int = 6) -> str:
        """FRED ``series/observations`` GET — requires ``FRED_API_KEY``."""
        if not config.FRED_API_KEY:
            raise ValueError("FRED_API_KEY is not set")
        # sort_order=desc → the response window contains the *newest* points;
        # parse_fred_json then picks the max-date observation from it.
        url = (
            "https://api.stlouisfed.org/fred/series/observations"
            f"?series_id={series_id}&api_key={config.FRED_API_KEY}"
            f"&file_type=json&sort_order=desc&limit={limit}"
        )
        return self._http_get(url)

    # ------------------------------------------------------------- cache
    def _bootstrap_from_cache(self) -> None:
        """Load the last good payload so a restart shows data immediately."""
        if not self._cache_file.exists():
            return
        try:
            doc = json.loads(self._cache_file.read_text(encoding="utf-8"))
            for ccy, r in (doc.get("rates") or {}).items():
                self._cached_rates[ccy] = MacroRate(
                    r["currency"], r["rate"], r["as_of"],
                    r.get("prev_rate"), r["source"], stale=True)
            self._last_fetch_ok = float(doc.get("fetched_at") or 0.0)
        except (OSError, ValueError, KeyError):
            log.exception("macro: cache bootstrap failed")

    def _store_cache(self, rates: dict[str, MacroRate]) -> None:
        """Persist the latest rates so a restart shows data immediately."""
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "fetched_at": self._last_fetch_ok,
                "rates": {
                    ccy: {"currency": r.currency, "rate": r.rate,
                          "as_of": r.as_of, "prev_rate": r.prev_rate,
                          "source": r.source}
                    for ccy, r in rates.items()
                },
            }
            self._cache_file.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            log.exception("macro: failed to write cache %s", self._cache_file)
