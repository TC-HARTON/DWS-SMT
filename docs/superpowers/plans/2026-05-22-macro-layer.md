# Macro / Rate-Differential Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a macro layer that fetches each currency's central-bank policy
rate (USD/EUR/GBP/JPY/AUD) plus US employment, computes a per-pair rate
differential and macro direction, shows them in a reference panel, and flags
DWS-SMT triggers that fight the carry.

**Architecture:** A new off-thread `macro` schedule (6 h) in the analysis loop
fetches five central-bank HTTP endpoints, parses them, caches to disk, and
publishes a `MacroSnapshot` to `LatestState`. Serialization ships `rates` +
`by_pair` (differential + direction). The front end paints a macro reference
panel and marks counter-carry DWS triggers. The macro filter lives entirely in
`macro_feed` + serialize + the front end — **`dws_smt.py` and
`indicator_engine.py` are not touched**.

**Tech Stack:** Python 3.11, `requests`, `xmltodict` (already deps), numpy,
pandas; vanilla-JS canvas front end; pytest.

**Spec:** `docs/superpowers/specs/2026-05-22-precision-optimization-design.md`
(Section B).

---

## Pre-flight notes for the implementer

- **Python interpreter:** the bare `python` command is a broken MS Store stub.
  Always use `C:\Users\ohuch\AppData\Local\Python\bin\python.exe`.
  Tests: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest tests/... -v`
- **Git:** the repo is under git on branch `feature/signal-validation-layer`
  (the signal-validation work is merged into this branch's history). Either
  continue on it or branch off it — confirm with the user. Each "Commit" step
  is a real commit.
- **Project memory rules:** no shortcuts / no placeholder stubs; bare `except`
  forbidden (catch specific exceptions, or `except Exception` + `# noqa: BLE001`
  only at an isolation boundary, matching `calendar_feed.py`); type hints +
  docstrings mandatory; tests must actually pass.
- **Integration-verification rule (learned the hard way):** after the macro
  schedule is wired, restart `main.py` and confirm the dashboard stays
  responsive while the macro worker runs. The macro fetch is pure HTTP and does
  NOT touch the MT5 connector lock, so it is far safer than the validation
  worker — but still verify.
- **Existing tests:** 184 currently pass. No task may break them.
- **Confirmed API formats** (researched 2026-05-22):
  - **FRED** — JSON, `{"observations":[{"date":"YYYY-MM-DD","value":"N"}, ...]}`.
    Needs `FRED_API_KEY`. The user has FRED set up.
  - **ECB** — SDMX `csvdata`: header has `...,TIME_PERIOD,OBS_VALUE,...`;
    parse by column name. Last row = latest.
  - **RBA** — F1.1 CSV: ~8 metadata rows (`Title,...`/`Description,...`/.../
    `Series ID,...`), then data rows `DD/MM/YYYY,<cash rate target>,...`.
    Column index 1 is the Cash Rate Target.
  - **BoE** — IADB CSV; returns **HTTP 403 to non-browser User-Agents** — the
    request MUST send a browser `User-Agent` header.
  - **BoJ** — `fm01_d_1_en.html`: an HTML page with a date/value table of the
    uncollateralised overnight call rate; rows are `YYYY/MM/DD` + value or `NA`.

---

## File Structure

| File | Responsibility |
|---|---|
| `config.py` (modify) | Macro endpoints, series IDs, currencies, `MACRO_REFRESH_SEC`, cache path, `FRED_API_KEY` |
| `analyzer/macro_feed.py` (create) | Fetch + parse 5 sources, disk cache, per-pair bias, `MacroEngine` |
| `tests/test_macro_feed.py` (create) | Parser tests with captured-sample fixtures, per-pair bias tests, engine test |
| `analyzer/state.py` (modify) | `set_macro` / `macro` snapshot slot |
| `analyzer/analysis_loop.py` (modify) | `macro` schedule + off-thread worker |
| `dashboard/serialize.py` (modify) | `serialize_macro` + wire into `snapshot_to_json` |
| `static/app.js` (modify) | Macro reference panel + counter-carry DWS trigger marker |
| `static/app.css` (modify) | Macro panel + counter-carry marker styling |

---

## Data model (Task 2 — referenced everywhere after)

```python
@dataclass(frozen=True)
class MacroRate:
    currency: str            # "USD" / "EUR" / "GBP" / "JPY" / "AUD"
    rate: float              # policy rate, percent
    as_of: str               # ISO date "YYYY-MM-DD"
    prev_rate: float | None  # the previous distinct rate, for trend detection
    source: str              # "fred" / "ecb" / "boe" / "boj" / "rba"
    stale: bool              # True when the last fetch failed (value from cache)

@dataclass(frozen=True)
class MacroEmployment:
    nonfarm_change: float | None        # latest MoM change in PAYEMS, thousands
    unemployment_rate: float | None     # latest UNRATE, percent
    as_of: str
    prev_nonfarm_change: float | None
    source: str                         # "fred"

@dataclass(frozen=True)
class MacroPairBias:
    pair: str                # "USDJPY"
    base_ccy: str            # "USD"
    quote_ccy: str           # "JPY"
    differential: float      # rate(base) - rate(quote), percent
    macro_dir: int           # +1 / 0 / -1
    label: str               # short JP label, e.g. "USD金利優位"

@dataclass(frozen=True)
class MacroSnapshot:
    generated_at: float
    fetched_at: float
    rates: dict[str, MacroRate]            # ccy -> MacroRate
    employment: MacroEmployment | None
    by_pair: dict[str, MacroPairBias]      # pair -> bias
    last_error: str | None
    consecutive_failures: int
```

---

## Task 1: Config constants

**Files:** Modify `config.py`

- [ ] **Step 1: Add the macro config block**

Insert after the signal-validation block (after `VALIDATION_STARTUP_DELAY_SEC`,
around `config.py:182`), before the `BIAS` block:

```python

# --------------------------------------------------------------------------- #
# Macro / rate-differential layer (precision-optimization spec, Section B)
# --------------------------------------------------------------------------- #
# Central-bank policy rates change ~8x/year on scheduled dates; a 6 h refresh
# catches a decision same-day at negligible cost. The fetch is pure HTTP (no
# MT5 connector lock) and runs off-thread, mirroring the calendar feed.
MACRO_REFRESH_SEC: Final[float] = 21600.0          # 6 hours
MACRO_FETCH_TIMEOUT_SEC: Final[float] = 20.0
MACRO_CACHE_FILE: Final[Path] = PROJECT_ROOT / "external" / "macro" / "macro_cache.json"
# FRED API key — read from env / .env, never hard-coded.
FRED_API_KEY: Final[str] = _get_env("FRED_API_KEY", "")
# A browser UA — the Bank of England IADB returns HTTP 403 to bot UAs.
MACRO_HTTP_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# Per-currency policy-rate source. Each entry: (source, url-or-series).
MACRO_FRED_RATE_SERIES: Final[str] = "DFEDTARU"      # Fed funds target, upper
MACRO_FRED_PAYEMS_SERIES: Final[str] = "PAYEMS"      # nonfarm payrolls, level
MACRO_FRED_UNRATE_SERIES: Final[str] = "UNRATE"      # unemployment rate
MACRO_ECB_URL: Final[str] = (
    "https://data-api.ecb.europa.eu/service/data/FM/"
    "D.U2.EUR.4F.KR.DFR.LEV?lastNObservations=8&format=csvdata"
)
MACRO_BOE_URL: Final[str] = (
    "https://www.bankofengland.co.uk/boeapps/database/fromshowcolumns.asp"
    "?csv.x=yes&Datefrom=01/Jan/2020&Dateto=now&SeriesCodes=IUDBEDR"
    "&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N"
)
MACRO_RBA_URL: Final[str] = "https://www.rba.gov.au/statistics/tables/csv/f1.1-data.csv"
MACRO_BOJ_URL: Final[str] = "https://www.stat-search.boj.or.jp/ssi/mtshtml/fm01_d_1_en.html"
# The five fiat currencies whose central-bank rates we track. XAU (gold) has
# no policy rate — handled specially in pair_macro_bias.
MACRO_CURRENCIES: Final[tuple[str, ...]] = ("USD", "EUR", "GBP", "JPY", "AUD")
```

- [ ] **Step 2: Verify the module imports**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -c "import config; print(config.MACRO_CURRENCIES, bool(config.MACRO_REFRESH_SEC))"`
Expected: `('USD', 'EUR', 'GBP', 'JPY', 'AUD') True`

- [ ] **Step 3: Ensure the cache directory is git-ignored**

Confirm `.gitignore` excludes the macro cache. Add this line under the
"logs / cache / runtime data" section of `.gitignore` if not already covered:

```
external/macro/*
```

- [ ] **Step 4: Commit**

```bash
git add config.py .gitignore
git commit -m "feat: add macro-layer config constants"
```

---

## Task 2: Data model + per-pair macro bias

**Files:** Create `analyzer/macro_feed.py`, create `tests/test_macro_feed.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_macro_feed.py`:

```python
"""Tests for the macro / rate-differential layer."""

from __future__ import annotations

import pytest

from analyzer import macro_feed as mf


def _rate(ccy, rate, prev=None, stale=False):
    return mf.MacroRate(currency=ccy, rate=rate, as_of="2026-05-22",
                        prev_rate=prev, source="test", stale=stale)


def test_pair_macro_bias_carry_direction():
    rates = {"USD": _rate("USD", 4.5), "JPY": _rate("JPY", 0.5)}
    b = mf.pair_macro_bias("USDJPY", rates)
    assert b.base_ccy == "USD" and b.quote_ccy == "JPY"
    assert b.differential == pytest.approx(4.0)
    assert b.macro_dir == 1            # USD out-yields JPY → carry favours USDJPY up


def test_pair_macro_bias_negative_carry():
    rates = {"EUR": _rate("EUR", 2.0), "GBP": _rate("GBP", 4.0)}
    b = mf.pair_macro_bias("EURGBP", rates)
    assert b.differential == pytest.approx(-2.0)
    assert b.macro_dir == -1


def test_pair_macro_bias_stale_currency_is_neutral():
    rates = {"USD": _rate("USD", 4.5, stale=True), "JPY": _rate("JPY", 0.5)}
    b = mf.pair_macro_bias("USDJPY", rates)
    assert b.macro_dir == 0            # never penalise on stale data


def test_pair_macro_bias_xauusd_uses_us_rate_trend():
    # Gold has no rate. Rising US rate → bearish gold → macro_dir -1.
    rates = {"USD": _rate("USD", 4.5, prev=4.25)}
    b = mf.pair_macro_bias("XAUUSD", rates)
    assert b.macro_dir == -1
    # Falling US rate → bullish gold.
    rates2 = {"USD": _rate("USD", 4.0, prev=4.25)}
    assert mf.pair_macro_bias("XAUUSD", rates2).macro_dir == 1
    # Flat → neutral.
    rates3 = {"USD": _rate("USD", 4.25, prev=4.25)}
    assert mf.pair_macro_bias("XAUUSD", rates3).macro_dir == 0


def test_pair_macro_bias_missing_currency_is_neutral():
    b = mf.pair_macro_bias("USDJPY", {"USD": _rate("USD", 4.5)})  # no JPY
    assert b.macro_dir == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest tests/test_macro_feed.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'analyzer.macro_feed'`

- [ ] **Step 3: Create the module with the data model and `pair_macro_bias`**

Create `analyzer/macro_feed.py`:

```python
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

import logging
from dataclasses import dataclass

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest tests/test_macro_feed.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add analyzer/macro_feed.py tests/test_macro_feed.py
git commit -m "feat: macro_feed data model + per-pair rate-differential bias"
```

---

## Task 3: Source parsers — pure functions

Each central-bank source has a different format. These five pure parsers turn
a raw response body into `(as_of: str, rate: float)`. They are unit-tested with
**fixtures captured from real responses** — Step 1 of this task instructs the
implementer to capture those samples first.

**Files:** Modify `analyzer/macro_feed.py`, modify `tests/test_macro_feed.py`,
create `tests/fixtures/macro/` with captured samples.

- [ ] **Step 1: Capture real response samples as fixtures**

Run each command and save the output as the named fixture file (create
`tests/fixtures/macro/`):

```bash
C:\Users\ohuch\AppData\Local\Python\bin\python.exe -c "import requests,config; print(requests.get(config.MACRO_ECB_URL,timeout=20).text)" > tests/fixtures/macro/ecb_sample.csv
C:\Users\ohuch\AppData\Local\Python\bin\python.exe -c "import requests,config; print(requests.get(config.MACRO_RBA_URL,timeout=20).text)" > tests/fixtures/macro/rba_sample.csv
C:\Users\ohuch\AppData\Local\Python\bin\python.exe -c "import requests,config; print(requests.get(config.MACRO_BOE_URL,headers={'User-Agent':config.MACRO_HTTP_USER_AGENT},timeout=20).text)" > tests/fixtures/macro/boe_sample.csv
C:\Users\ohuch\AppData\Local\Python\bin\python.exe -c "import requests,config; print(requests.get(config.MACRO_BOJ_URL,headers={'User-Agent':config.MACRO_HTTP_USER_AGENT},timeout=20).text)" > tests/fixtures/macro/boj_sample.html
```

Open each fixture and confirm the format matches the description in the
"Confirmed API formats" pre-flight note. If a source's live format differs from
the parser code below, adjust the parser to match the captured sample (the
sample is the source of truth). If BoE still returns 403 even with the browser
User-Agent, note it — the engine (Task 4) degrades that currency to `stale`.

- [ ] **Step 2: Write the failing parser tests**

Append to `tests/test_macro_feed.py`:

```python
# ----------------------------------------------------------------- parsers
import pathlib

_FIX = pathlib.Path(__file__).parent / "fixtures" / "macro"


def test_parse_ecb_csv():
    body = (_FIX / "ecb_sample.csv").read_text(encoding="utf-8")
    as_of, rate = mf.parse_ecb_csv(body)
    assert len(as_of) == 10 and as_of[4] == "-"      # ISO date
    assert isinstance(rate, float)


def test_parse_rba_csv():
    body = (_FIX / "rba_sample.csv").read_text(encoding="utf-8")
    as_of, rate = mf.parse_rba_csv(body)
    assert len(as_of) == 10 and as_of[4] == "-"
    assert isinstance(rate, float)


def test_parse_fred_json():
    body = ('{"observations":[{"date":"2026-03-01","value":"4.25"},'
            '{"date":"2026-04-01","value":"4.50"}]}')
    as_of, rate = mf.parse_fred_json(body)
    assert as_of == "2026-04-01"
    assert rate == pytest.approx(4.50)


def test_parse_fred_json_skips_missing():
    # FRED uses "." for a missing value — the parser must skip it.
    body = ('{"observations":[{"date":"2026-03-01","value":"4.25"},'
            '{"date":"2026-04-01","value":"."}]}')
    as_of, rate = mf.parse_fred_json(body)
    assert as_of == "2026-03-01"
    assert rate == pytest.approx(4.25)


def test_parse_boj_html():
    body = (_FIX / "boj_sample.html").read_text(encoding="utf-8")
    as_of, rate = mf.parse_boj_html(body)
    assert len(as_of) == 10 and as_of[4] == "-"
    assert isinstance(rate, float)
```

(No BoE parser test here — BoE shares `parse_boe_csv`; add a test for it only
if the captured `boe_sample.csv` is a real CSV. If BoE returned 403, skip the
BoE test and leave a comment, the engine handles the failure.)

- [ ] **Step 3: Run tests to verify they fail**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest tests/test_macro_feed.py -k parse -v`
Expected: FAIL — `AttributeError: module 'analyzer.macro_feed' has no attribute 'parse_ecb_csv'`

- [ ] **Step 4: Implement the parsers**

Append to `analyzer/macro_feed.py` (after `pair_macro_bias`). Add `import csv`,
`import io`, `import json`, `import re` to the imports at the top of the file:

```python
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


def parse_rba_csv(body: str) -> tuple[str, float]:
    """Parse the RBA F1.1 CSV → (latest date ISO, Cash Rate Target).

    The file has ~8 metadata rows (``Title``/``Description``/.../``Series ID``)
    then data rows ``DD/MM/YYYY,<cash rate target>,...``. Column index 1 is the
    Cash Rate Target. The date is converted to ISO ``YYYY-MM-DD``.
    """
    meta_keys = {"title", "description", "frequency", "type", "units",
                 "source", "publication date", "series id", ""}
    last_date = ""
    last_rate: float | None = None
    for row in csv.reader(io.StringIO(body)):
        if not row or row[0].strip().lower() in meta_keys:
            continue
        if len(row) < 2 or not (row[1] or "").strip():
            continue
        d = row[0].strip()                      # DD/MM/YYYY
        try:
            dd, mm, yyyy = d.split("/")
            iso = f"{yyyy}-{mm.zfill(2)}-{dd.zfill(2)}"
            rate = float(row[1])
        except (ValueError, IndexError):
            continue
        last_date, last_rate = iso, rate
    if last_rate is None:
        raise ValueError("RBA CSV had no usable Cash Rate Target row")
    return last_date, last_rate


def parse_boe_csv(body: str) -> tuple[str, float]:
    """Parse the BoE IADB CSV (series IUDBEDR) → (latest date ISO, rate).

    The IADB CSV is ``DATE,IUDBEDR`` with dates like ``01 Jan 2024``. The last
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
        # IADB dates are "DD Mon YYYY"; normalise to ISO.
        try:
            from datetime import datetime
            iso = datetime.strptime(d, "%d %b %Y").strftime("%Y-%m-%d")
        except ValueError:
            iso = d
        last_date, last_rate = iso, rate
    if last_rate is None:
        raise ValueError("BoE CSV had no usable IUDBEDR row")
    return last_date, last_rate


def parse_boj_html(body: str) -> tuple[str, float]:
    """Parse the BoJ ``fm01_d_1_en.html`` page → (latest date ISO, call rate).

    The page embeds a table of ``YYYY/MM/DD`` + value (or ``NA``). The most
    recent non-``NA`` row is the uncollateralised overnight call rate, the
    BoJ policy-rate proxy. Parsed with a regex over the table cells so no HTML
    library is needed.
    """
    # Match a YYYY/MM/DD cell followed (within the same row) by a numeric cell.
    pattern = re.compile(
        r"(\d{4})/(\d{2})/(\d{2})\s*</[^>]+>\s*<[^>]+>\s*"
        r"(-?\d+\.\d+|NA)",
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
```

Note: the BoJ regex depends on the captured `boj_sample.html` cell layout. If
the captured sample's table markup differs, adjust the regex to match the real
`<td>`/`<th>` structure around the date and value cells — the fixture is the
source of truth.

- [ ] **Step 5: Run tests to verify they pass**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest tests/test_macro_feed.py -v`
Expected: PASS (all parser + bias tests)

- [ ] **Step 6: Commit**

```bash
git add analyzer/macro_feed.py tests/test_macro_feed.py tests/fixtures/macro/
git commit -m "feat: macro source parsers (FRED/ECB/RBA/BoE/BoJ) with fixtures"
```

---

## Task 4: `MacroEngine` — fetch, cache, build the snapshot

Mirrors `CalendarEngine` (`analyzer/calendar_feed.py`): an HTTP fetch with a
timeout and retries, a disk cache so a restart shows the last payload, and
per-source isolation so one central bank failing only marks that currency
`stale`.

**Files:** Modify `analyzer/macro_feed.py`, modify `tests/test_macro_feed.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_macro_feed.py`:

```python
# ------------------------------------------------------------------ engine
def test_macro_engine_compute_with_stub(monkeypatch, tmp_path):
    # Stub every HTTP fetch so the test is offline + deterministic.
    fake = {
        "USD": ("2026-05-01", 4.50), "EUR": ("2026-05-01", 2.00),
        "GBP": ("2026-05-01", 4.25), "JPY": ("2026-05-01", 0.50),
        "AUD": ("2026-05-01", 4.10),
    }
    eng = mf.MacroEngine(cache_file=tmp_path / "macro_cache.json")
    monkeypatch.setattr(eng, "_fetch_rate",
                        lambda ccy: mf.MacroRate(ccy, fake[ccy][1], fake[ccy][0],
                                                 None, "test", False))
    monkeypatch.setattr(eng, "_fetch_employment", lambda: None)
    snap = eng.compute()

    assert isinstance(snap, mf.MacroSnapshot)
    assert set(snap.rates) == {"USD", "EUR", "GBP", "JPY", "AUD"}
    assert "USDJPY" in snap.by_pair
    assert snap.by_pair["USDJPY"].macro_dir == 1          # 4.50 > 0.50
    assert snap.consecutive_failures == 0


def test_macro_engine_one_source_failure_is_isolated(monkeypatch, tmp_path):
    def flaky(ccy):
        if ccy == "JPY":
            raise ValueError("boj down")
        return mf.MacroRate(ccy, 4.0, "2026-05-01", None, "test", False)
    eng = mf.MacroEngine(cache_file=tmp_path / "macro_cache.json")
    monkeypatch.setattr(eng, "_fetch_rate", flaky)
    monkeypatch.setattr(eng, "_fetch_employment", lambda: None)
    snap = eng.compute()
    # JPY missing entirely OR present but stale — either way pairs with JPY
    # must be neutral, and the other currencies are unaffected.
    assert snap.by_pair["USDJPY"].macro_dir == 0
    assert snap.by_pair["EURGBP"].macro_dir == 0          # 4.0 - 4.0 == 0
    assert "USD" in snap.rates
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest tests/test_macro_feed.py -k engine -v`
Expected: FAIL — `AttributeError: module 'analyzer.macro_feed' has no attribute 'MacroEngine'`

- [ ] **Step 3: Implement `MacroEngine`**

Append to `analyzer/macro_feed.py`. Add `import time` and `from pathlib import
Path` and `import requests` to the imports:

```python
# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class MacroEngine:
    """Fetches every macro source, caches to disk, builds a MacroSnapshot.

    Per-source isolation: a single central bank failing marks only that
    currency ``stale`` (value re-used from cache when available); the rest of
    the snapshot is unaffected. Mirrors :class:`analyzer.calendar_feed.CalendarEngine`.
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
            log.warning("macro: %d source error(s) — %s",
                        len(errors), self._last_error)
        else:
            self._consecutive_failures = 0
            self._last_error = None
            self._last_fetch_ok = time.time()

        if rates:
            self._cached_rates = dict(rates)
            self._store_cache(rates, employment)

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
        elif ccy == "EUR":
            as_of, rate = parse_ecb_csv(self._http_get(config.MACRO_ECB_URL))
            source = "ecb"
        elif ccy == "GBP":
            as_of, rate = parse_boe_csv(self._http_get(config.MACRO_BOE_URL))
            source = "boe"
        elif ccy == "JPY":
            as_of, rate = parse_boj_html(self._http_get(config.MACRO_BOJ_URL))
            source = "boj"
        elif ccy == "AUD":
            as_of, rate = parse_rba_csv(self._http_get(config.MACRO_RBA_URL))
            source = "rba"
        else:
            raise ValueError(f"no macro source for {ccy}")
        prev = self._cached_rates.get(ccy)
        prev_rate = (prev.rate if prev is not None and prev.rate != rate
                     else (prev.prev_rate if prev is not None else None))
        return MacroRate(ccy, rate, as_of, prev_rate, source, stale=False)

    def _fetch_employment(self) -> MacroEmployment | None:
        """Fetch US nonfarm-payroll change + unemployment rate from FRED."""
        payems = parse_fred_json(self._fred_get(config.MACRO_FRED_PAYEMS_SERIES,
                                                limit=24))
        unrate = parse_fred_json(self._fred_get(config.MACRO_FRED_UNRATE_SERIES))
        # parse_fred_json returns only the latest; for the NFP *change* we need
        # the last two PAYEMS points — fetch them explicitly.
        as_of, nfp_chg, prev_chg = self._fred_payems_change()
        return MacroEmployment(
            nonfarm_change=nfp_chg,
            unemployment_rate=unrate[1],
            as_of=as_of,
            prev_nonfarm_change=prev_chg,
            source="fred",
        )

    def _fred_payems_change(self) -> tuple[str, float | None, float | None]:
        """Latest + previous month-over-month change in PAYEMS (thousands)."""
        doc = __import__("json").loads(
            self._fred_get(config.MACRO_FRED_PAYEMS_SERIES, limit=4))
        vals = [(o["date"][:10], float(o["value"]))
                for o in doc.get("observations", [])
                if (o.get("value") or "").strip() not in ("", ".")]
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
        url = (
            "https://api.stlouisfed.org/fred/series/observations"
            f"?series_id={series_id}&api_key={config.FRED_API_KEY}"
            f"&file_type=json&sort_order=asc&limit={limit}"
        )
        return self._http_get(url)

    # ------------------------------------------------------------- cache
    def _bootstrap_from_cache(self) -> None:
        """Load the last good payload so a restart shows data immediately."""
        if not self._cache_file.exists():
            return
        try:
            import json
            doc = json.loads(self._cache_file.read_text(encoding="utf-8"))
            for ccy, r in (doc.get("rates") or {}).items():
                self._cached_rates[ccy] = MacroRate(
                    r["currency"], r["rate"], r["as_of"],
                    r.get("prev_rate"), r["source"], stale=True)
            self._last_fetch_ok = float(doc.get("fetched_at") or 0.0)
        except (OSError, ValueError, KeyError):
            log.exception("macro: cache bootstrap failed")

    def _store_cache(self, rates: dict[str, MacroRate],
                     employment: MacroEmployment | None) -> None:
        try:
            import json
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
```

Note: `_fetch_employment` calls `parse_fred_json` for UNRATE and uses
`_fred_payems_change` for the NFP change. The unused `payems` line in the draft
is dead — remove it; keep only the `unrate` fetch and the
`_fred_payems_change()` call. (Implementer: write `_fetch_employment` as just
the unrate fetch + the change call + the return.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest tests/test_macro_feed.py -v`
Expected: PASS (all macro tests)

- [ ] **Step 5: Run the whole suite**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest -q`
Expected: PASS (184 existing + new macro tests, 0 failures)

- [ ] **Step 6: Commit**

```bash
git add analyzer/macro_feed.py tests/test_macro_feed.py
git commit -m "feat: MacroEngine — fetch, cache, per-source isolation"
```

---

## Task 5: Wire `MacroSnapshot` into `LatestState`

**Files:** Modify `analyzer/state.py`, modify `tests/test_state_and_serialize.py`

Follow the EXACT pattern of the `calendar` / `validation` slots already in
`state.py`. Macro is a heavy domain → the writer bumps both
`_monotonic_version` and `_analysis_version`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_state_and_serialize.py`:

```python
def test_state_set_and_read_macro():
    from analyzer.state import LatestState
    from analyzer.macro_feed import MacroSnapshot

    st = LatestState()
    assert st.macro is None
    before = st.analysis_version
    snap = MacroSnapshot(generated_at=1.0, fetched_at=1.0, rates={},
                         employment=None, by_pair={}, last_error=None,
                         consecutive_failures=0)
    st.set_macro(snap)
    assert st.macro is snap
    assert st.analysis_version == before + 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest tests/test_state_and_serialize.py::test_state_set_and_read_macro -v`
Expected: FAIL — `AttributeError: 'LatestState' object has no attribute 'macro'`

- [ ] **Step 3: Add the state slot**

In `analyzer/state.py`, make these five edits, each mirroring the existing
`validation` slot:

1. Import (near the other analyzer imports):
```python
from analyzer.macro_feed import MacroSnapshot
```
2. Field in `__init__` (after `self._validation`):
```python
        self._macro: Optional[MacroSnapshot] = None
```
3. Writer (after `set_validation`):
```python
    def set_macro(self, snapshot: MacroSnapshot) -> None:
        with self._cond:
            self._macro = snapshot
            self._monotonic_version += 1
            self._analysis_version += 1
            self._cond.notify_all()
```
4. Reader (after the `validation` property):
```python
    @property
    def macro(self) -> MacroSnapshot | None:
        with self._lock:
            return self._macro
```
5. Add to the `snapshot()` dict (after `"validation"`):
```python
                "macro": self._macro,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest tests/test_state_and_serialize.py::test_state_set_and_read_macro -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add analyzer/state.py tests/test_state_and_serialize.py
git commit -m "feat: LatestState macro snapshot slot"
```

---

## Task 6: `macro` schedule + off-thread worker

Mirror the `calendar` job in `analyzer/analysis_loop.py` exactly: a
`threading.Event` in-flight guard plus a daemon worker. The macro fetch is pure
HTTP (no MT5 connector lock), so it cannot starve the price tick the way the
validation worker did — but it still runs off-thread so a slow HTTP call never
blocks the loop.

**Files:** Modify `analyzer/analysis_loop.py`

- [ ] **Step 1: Add the import**

After the `signal_validator` import, add:
```python
from analyzer.macro_feed import MacroEngine
```

- [ ] **Step 2: Constructor parameters + state**

In `AnalysisLoop.__init__` signature, add after `signal_validator`:
```python
        macro_engine: MacroEngine | None = None,
```
and after `validation_interval`:
```python
        macro_interval: float = config.MACRO_REFRESH_SEC,
```
In the constructor body, after the `_validation_inflight` line:
```python
        self._macro_engine = macro_engine or MacroEngine()
        self._macro_inflight = threading.Event()
```
In `self._schedules`, add after the `validation` entry:
```python
            _Schedule("macro", macro_interval),
```

- [ ] **Step 3: Register the dispatch handler**

In `_dispatch`, add to the `handler` dict after `"validation"`:
```python
            "macro": self._do_macro_refresh,
```

- [ ] **Step 4: Add the handler + worker**

After `_validation_refresh_worker`, add:
```python
    def _do_macro_refresh(self, bases: list[str]) -> None:
        """Spec Section B: refresh central-bank rates + employment every 6 h.

        Dispatched to a daemon worker — the HTTP fetch can take up to
        MACRO_FETCH_TIMEOUT_SEC per source. The fetch is plain HTTP and never
        touches the MT5 connector, so it cannot starve the price tick; the
        off-thread dispatch only keeps a slow network call out of the loop.
        """
        if self._macro_inflight.is_set():
            log.debug("macro: previous fetch still in flight, skipping tick")
            return
        self._macro_inflight.set()
        worker = threading.Thread(
            target=self._macro_refresh_worker,
            name="macro-fetch", daemon=True,
        )
        worker.start()

    def _macro_refresh_worker(self) -> None:
        try:
            self._state.set_macro(self._macro_engine.compute())
        except Exception:               # noqa: BLE001 — never reach the loop
            log.exception("macro worker failed")
        finally:
            self._macro_inflight.clear()
```

- [ ] **Step 5: Run the suite**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest -q`
Expected: PASS (no regressions).
Also: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -c "import analyzer.analysis_loop"` — no error.

- [ ] **Step 6: Commit**

```bash
git add analyzer/analysis_loop.py
git commit -m "feat: off-thread macro-refresh schedule in the analysis loop"
```

---

## Task 7: Serialize `MacroSnapshot`

**Files:** Modify `dashboard/serialize.py`, modify `tests/test_state_and_serialize.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_state_and_serialize.py`:

```python
def test_serialize_macro_shape():
    from dashboard.serialize import serialize_macro
    from analyzer.macro_feed import (
        MacroRate, MacroPairBias, MacroSnapshot,
    )
    rates = {"USD": MacroRate("USD", 4.5, "2026-05-01", 4.25, "fred", False)}
    pair = MacroPairBias("USDJPY", "USD", "JPY", 4.0, 1, "USD金利優位")
    snap = MacroSnapshot(generated_at=1.0, fetched_at=1.0, rates=rates,
                         employment=None, by_pair={"USDJPY": pair},
                         last_error=None, consecutive_failures=0)
    out = serialize_macro(snap)
    assert out["rates"]["USD"]["rate"] == 4.5
    assert out["by_pair"]["USDJPY"]["macro_dir"] == 1
    assert out["by_pair"]["USDJPY"]["differential"] == 4.0
    assert serialize_macro(None) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest tests/test_state_and_serialize.py -k macro -v`
Expected: FAIL — `ImportError: cannot import name 'serialize_macro'`

- [ ] **Step 3: Add the serializer**

In `dashboard/serialize.py`:

1. Add the import after the `signal_validator` import:
```python
from analyzer.macro_feed import (
    MacroEmployment,
    MacroPairBias,
    MacroRate,
    MacroSnapshot,
)
```
2. Add the serializer functions after `serialize_validation`:
```python
# --------------------------------------------------------------------------- #
# Macro / rate-differential layer (precision-optimization spec, Section B)
# --------------------------------------------------------------------------- #

def _serialize_macro_rate(r: MacroRate) -> dict[str, Any]:
    return {
        "currency": r.currency,
        "rate": _opt_float(r.rate),
        "as_of": r.as_of,
        "prev_rate": _opt_float(r.prev_rate),
        "source": r.source,
        "stale": bool(r.stale),
    }


def _serialize_macro_pair(b: MacroPairBias) -> dict[str, Any]:
    return {
        "pair": b.pair,
        "base_ccy": b.base_ccy,
        "quote_ccy": b.quote_ccy,
        "differential": _opt_float(b.differential),
        "macro_dir": int(b.macro_dir),
        "label": b.label,
    }


def _serialize_macro_employment(e: MacroEmployment | None) -> dict[str, Any] | None:
    if e is None:
        return None
    return {
        "nonfarm_change": _opt_float(e.nonfarm_change),
        "unemployment_rate": _opt_float(e.unemployment_rate),
        "as_of": e.as_of,
        "prev_nonfarm_change": _opt_float(e.prev_nonfarm_change),
        "source": e.source,
    }


def serialize_macro(s: MacroSnapshot | None) -> dict[str, Any] | None:
    """Serialise the macro snapshot for the WebSocket payload."""
    if s is None:
        return None
    return {
        "generated_at": float(s.generated_at),
        "fetched_at": float(s.fetched_at) if s.fetched_at > 0 else None,
        "rates": {ccy: _serialize_macro_rate(r) for ccy, r in s.rates.items()},
        "employment": _serialize_macro_employment(s.employment),
        "by_pair": {p: _serialize_macro_pair(b) for p, b in s.by_pair.items()},
        "last_error": s.last_error,
        "consecutive_failures": int(s.consecutive_failures),
    }
```
3. Wire into `snapshot_to_json` — add after the `"validation"` line:
```python
        "macro": serialize_macro(snap["macro"]),  # type: ignore[arg-type]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest tests/test_state_and_serialize.py -k macro -v`
Expected: PASS

- [ ] **Step 5: Run the whole suite**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest -q`
Expected: PASS (0 failures)

- [ ] **Step 6: Commit**

```bash
git add dashboard/serialize.py tests/test_state_and_serialize.py
git commit -m "feat: serialize MacroSnapshot for the WS payload"
```

---

## Task 8: Macro reference panel (front end)

Add a "MACRO / 金利差" panel listing each pair's rate differential and macro
direction, plus a US-employment context line. Follow the existing panel
patterns in `static/app.js` (the strength / correlation / calendar panels).

**Files:** Modify `static/app.js`, modify `static/app.css`

- [ ] **Step 1: Locate the panel structure**

Read `static/app.js` and find how `paintStrength` / `paintCorrelationList` /
`paintCalendar` are defined and where they are called from `paintAll`. The
macro panel follows the same shape: a `paintMacro(snap)` painter, gated by
`changed('macro', snap.macro && snap.macro.generated_at)`, writing into a
`data-bind` host element. Find the `index.html` / layout region where the
strength/correlation/calendar panel host elements live and add a sibling host
for the macro panel.

- [ ] **Step 2: Add the macro panel host**

In `static/index.html` (or wherever the strength/correlation/calendar panel
hosts are declared), add a panel section near the calendar panel:

```html
<section class="panel macro-panel">
  <h2>MACRO / 金利差</h2>
  <div data-bind="macro"></div>
</section>
```

- [ ] **Step 3: Add `paintMacro`**

Add to `static/app.js` (near `paintCalendar`):

```javascript
/** Paint the macro / rate-differential reference panel.
 *  One row per pair: base rate, quote rate, differential, macro direction. */
function paintMacro(snap) {
    const m = snap.macro;
    const root = $bind('macro');
    if (!root) return;
    if (!changed('macro', m && m.generated_at)) return;
    if (!m || !m.rates || Object.keys(m.rates).length === 0) {
        root.innerHTML = '<div class="empty mute">マクロデータ未取得</div>';
        return;
    }
    const rate = ccy => {
        const r = m.rates[ccy];
        return r ? (r.rate.toFixed(2) + (r.stale ? '*' : '')) : '--';
    };
    const arrow = d => d > 0 ? '▲' : d < 0 ? '▼' : '·';
    const rows = (SYMBOL_ORDER || []).map(sym => {
        const b = m.by_pair && m.by_pair[sym];
        if (!b) return '';
        const dirCls = b.macro_dir > 0 ? 'pos'
                     : b.macro_dir < 0 ? 'neg' : 'mute';
        const diff = b.differential == null ? '--'
                   : (b.differential >= 0 ? '+' : '') + b.differential.toFixed(2);
        return `<div class="macro-row">
            <span class="macro-pair">${esc(sym)}</span>
            <span class="macro-rate">${esc(rate(b.base_ccy))}</span>
            <span class="macro-rate">${esc(rate(b.quote_ccy))}</span>
            <span class="macro-diff ${dirCls}">${esc(diff)}</span>
            <span class="macro-dir ${dirCls}">${arrow(b.macro_dir)} ${esc(b.label)}</span>
        </div>`;
    }).join('');
    let emp = '';
    if (m.employment) {
        const e = m.employment;
        const nfp = e.nonfarm_change == null ? '--'
                  : (e.nonfarm_change >= 0 ? '+' : '') + Math.round(e.nonfarm_change);
        emp = `<div class="macro-emp">米雇用 NFP変化 ${esc(nfp)}k ·
               失業率 ${e.unemployment_rate == null ? '--'
               : esc(e.unemployment_rate.toFixed(1)) + '%'}</div>`;
    }
    root.innerHTML = rows + emp;
}
```

- [ ] **Step 4: Call `paintMacro` from `paintAll`**

In `paintAll`, add `paintMacro(latestSnap);` next to the other panel painters
(e.g. right after `paintCalendar(latestSnap);`).

- [ ] **Step 5: Add the CSS**

Append to `static/app.css`:

```css
/* Macro / rate-differential panel */
.macro-row {
    display: grid;
    grid-template-columns: 70px 52px 52px 56px 1fr;
    gap: 6px;
    font-size: 11px;
    padding: 2px 4px;
    align-items: baseline;
    color: #f2f4f9;
}
.macro-row:nth-child(odd) { background: rgba(255, 255, 255, 0.02); }
.macro-pair { font-weight: 700; }
.macro-rate { color: #8089a0; text-align: right; }
.macro-diff { text-align: right; font-weight: 700; }
.macro-diff.pos, .macro-dir.pos { color: #00d09c; }
.macro-diff.neg, .macro-dir.neg { color: #ff5b6b; }
.macro-diff.mute, .macro-dir.mute { color: #8089a0; }
.macro-emp {
    font-size: 11px;
    color: #f2f4f9;
    padding: 4px;
    margin-top: 4px;
    border-top: 1px solid rgba(255, 255, 255, 0.06);
}
```

- [ ] **Step 6: Verify in the browser**

Start `main.py`, load `http://127.0.0.1:8050` with the browse skill, confirm
the MACRO panel renders one row per pair with rates + differential + direction.
`$B console` shows no errors. (The first macro fetch fires at startup; until it
completes the panel shows "マクロデータ未取得".)

- [ ] **Step 7: Commit**

```bash
git add static/app.js static/app.css static/index.html
git commit -m "feat: macro / rate-differential reference panel"
```

---

## Task 9: Counter-carry DWS trigger marker

When a DWS-SMT BUY/SELL trigger fights the pair's macro direction, flag it. The
existing trigger draw in `drawDwsCanvas` already filters by BIAS
(`dwsTriggerTradeable`); the macro check sits right next to it.

**Files:** Modify `static/app.js`, modify `static/app.css`

- [ ] **Step 1: Add a macro-alignment helper**

Add to `static/app.js` near `dwsTriggerTradeable`:

```javascript
/** Macro alignment of a BUY/SELL trigger for *sym*: +1 aligned with the carry,
 *  -1 counter-carry, 0 when there is no macro data. EXIT is direction-neutral. */
function dwsTriggerMacroAlign(g, sym, snap) {
    if (g !== 'BUY' && g !== 'SELL') return 0;
    const b = snap.macro && snap.macro.by_pair && snap.macro.by_pair[sym];
    if (!b || !b.macro_dir) return 0;
    const triggerDir = g === 'BUY' ? 1 : -1;
    return triggerDir === b.macro_dir ? 1 : -1;
}
```

- [ ] **Step 2: Flag counter-carry markers in `drawDwsCanvas`**

In `drawDwsCanvas`, find the trigger-drawing loop (where `drawDwsMarker` is
called). After computing `tradeable`, add:

```javascript
        const macroAlign = dwsTriggerMacroAlign(g, sym, snap);
```

Then, when `macroAlign < 0` (counter-carry), draw a small warning glyph above
the marker. Right after the `drawDwsMarker(...)` call, add:

```javascript
        if (macroAlign < 0) {
            ctx.fillStyle = '#ffb74d';
            ctx.font = '700 9px monospace';
            ctx.textAlign = 'center'; ctx.textBaseline = 'bottom';
            ctx.fillText('逆', cx, plotY - 1);
        }
```

- [ ] **Step 3: Note the counter-carry triggers in the state line**

In `updateDwsSync` (or `updateDwsState`), if the latest trigger is
counter-carry, append a short note. Read the current `updateDwsSync` body and
add, after the existing text is built, a check: if
`dwsTriggerMacroAlign(latestTriggerSide, sym, snap) < 0`, append
`「マクロ逆行」` to the text and add a CSS class `macro-counter`.

- [ ] **Step 4: Add CSS for the counter note**

Append to `static/app.css`:

```css
.dws-sync.macro-counter { color: #ffb74d; }
```

- [ ] **Step 5: Verify in the browser**

Restart `main.py`, load the dashboard, expand a few symbols, and confirm: when
a pair's macro direction opposes a DWS BUY/SELL trigger, the `逆` glyph appears
above that trigger. `$B console` shows no errors.

- [ ] **Step 6: Commit**

```bash
git add static/app.js static/app.css
git commit -m "feat: flag counter-carry DWS-SMT triggers against macro direction"
```

---

## Task 10: Full verification

- [ ] **Step 1: Run the entire test suite**

Run: `C:\Users\ohuch\AppData\Local\Python\bin\python.exe -m pytest -q`
Expected: PASS, 0 failures.

- [ ] **Step 2: Restart the server, confirm a clean boot**

Run `main.py`. Confirm: no exceptions; within a minute a macro fetch completes
(or logs a per-source warning without crashing). If `FRED_API_KEY` is unset,
USD + employment will be `stale` — that is expected, not a crash.

- [ ] **Step 3: Integration-responsiveness check**

While the macro worker runs its first fetch, confirm the dashboard HTTP stays
responsive:
`C:\Users\ohuch\AppData\Local\Python\bin\python.exe -c "import urllib.request,time; t=time.time(); urllib.request.urlopen('http://127.0.0.1:8050/',timeout=8).read(); print('HTTP ok in',round(time.time()-t,2),'s')"`
Expected: a fast response — the macro fetch is HTTP-only and off-thread, so it
must not affect responsiveness.

- [ ] **Step 4: Browser smoke test**

With the browse skill: load the dashboard, confirm the MACRO panel renders
per-pair differentials, expand a symbol and confirm the counter-carry `逆`
glyph logic works, `$B console` shows no errors.

- [ ] **Step 5: Confirm SPEC §19 budget intact**

`latestSnap.analysis.compute_ms` stays under 50 ms in steady state — the macro
layer adds no work to the indicator compute path.

- [ ] **Step 6: Final commit**

```bash
git add -A
git commit -m "feat: macro / rate-differential layer complete"
```

---

## Self-review notes (for the implementer)

- **Spec coverage:** B.2 sources — Task 1 (config) + Task 3/4 (parsers +
  engine). B.3 data model — Task 2. B.4 per-pair bias incl. XAUUSD special
  case — Task 2. B.5 trigger filter at the trigger layer (front end, not
  `dws_smt.py`) — Task 9. B.6 reference panel — Task 8. B.7 cadence — Task 1 +
  Task 6. B.8 files — all covered. B.9 per-source error isolation — Task 4.
  B.10 tests — Tasks 2-4, 5, 7.
- **`dws_smt.py` / `indicator_engine.py` are intentionally untouched** — the
  macro filter is data (macro_feed) + serialize + front end only.
- **Honest limitation** (carry vs rate-expectation momentum) is documented in
  the `macro_feed.py` module docstring per spec B.4.
- **FRED_API_KEY**: if unset, USD rate + employment degrade to `stale`/`None`
  gracefully — the dashboard still works, USD pairs just show neutral macro.
- **BoE 403 risk**: the browser User-Agent is set; if BoE still blocks, GBP
  degrades to `stale` and GBP pairs show neutral macro — no crash.
