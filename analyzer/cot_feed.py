"""CFTC Commitment of Traders (COT) feed — gold-futures speculative positioning.

The CFTC publishes a weekly Commitments of Traders report (Tuesday snapshot,
released the following Friday). This module pulls the **Legacy Futures-Only**
report for COMEX gold from the CFTC's public Socrata API (no auth) and derives
the large-speculator (non-commercial) net position, its week-over-week change,
its position within the trailing 1-year range, and the commercial (hedger) net.

Why it matters for gold: large speculators (managed money / funds) drive
momentum, commercials hedge against it. An extreme spec net-long is a *crowded*
trade — a contrarian caution, not a trade signal. Display-only context; this
module never feeds trigger / trade / order logic.

Every fetch is plain HTTP — this module never touches the MT5 connector. It is
defensive: on any upstream failure it reuses the last-good value (flagged
``stale``) rather than raising into the analysis loop, mirroring
:class:`analyzer.macro_feed.MacroEngine`.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import requests

import config

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CotSnapshot:
    """One COT refresh — COMEX gold large-spec + commercial positioning.

    ``net`` is the large-speculator (non-commercial) net long contracts
    (long − short); ``comm_net`` is the commercial/hedger net (the structural
    mirror). ``pctile_1y`` is the percentile rank of the current ``net`` within
    the trailing window (0–100; ~100 = net longs at a 1-year high). ``extreme``
    flags a crowded book: +1 net-long extreme, −1 net-short extreme, 0 normal.
    ``net_history`` / ``history_dates`` are parallel chronological arrays for a
    sparkline.
    """

    market: str
    report_date: str                 # ISO date of the latest (Tuesday) report
    noncomm_long: int | None
    noncomm_short: int | None
    net: int | None                  # large-spec net = long − short
    net_prev: int | None             # prior week's net
    net_change: int | None           # net − net_prev (week-over-week)
    comm_long: int | None
    comm_short: int | None
    comm_net: int | None             # commercial/hedger net
    open_interest: int | None
    net_pct_oi: float | None         # net / OI * 100
    long_share: float | None         # long / (long + short) * 100
    pctile_1y: float | None          # percentile of net within the window (0–100)
    direction: int                   # +1 spec net long / −1 net short / 0
    extreme: int                     # +1 crowded long / −1 crowded short / 0
    net_history: tuple[int, ...]     # chronological large-spec net per week
    history_dates: tuple[str, ...]   # parallel ISO report dates
    fetched_at: float                # epoch seconds of the last successful fetch
    generated_at: float              # epoch seconds this snapshot was built
    stale: bool                      # True when reused from cache (fetch failed)
    last_error: str | None


def _empty_snapshot(last_error: str | None) -> CotSnapshot:
    """A fully-empty stale snapshot (no data yet / total failure, no cache)."""
    return CotSnapshot(
        market=config.COT_GOLD_MARKET,
        report_date="",
        noncomm_long=None, noncomm_short=None, net=None,
        net_prev=None, net_change=None,
        comm_long=None, comm_short=None, comm_net=None,
        open_interest=None, net_pct_oi=None, long_share=None,
        pctile_1y=None, direction=0, extreme=0,
        net_history=(), history_dates=(),
        fetched_at=0.0, generated_at=time.time(),
        stale=True, last_error=last_error,
    )


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def _to_int(value: object) -> int | None:
    """Coerce a Socrata field (string/number) to int, or None if unusable."""
    if value is None:
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _pct_rank(values: list[int], x: int) -> float | None:
    """Percentile rank of *x* within *values* (0–100, inclusive ``<=``).

    Returns 100.0 when *x* is the maximum of the window (net longs at a 1-year
    high) and ~0 at the minimum. None for an empty window.
    """
    if not values:
        return None
    le = sum(1 for v in values if v <= x)
    return le / len(values) * 100.0


def parse_cot_rows(rows: list[dict]) -> CotSnapshot:
    """Build a :class:`CotSnapshot` from raw Socrata rows (any order).

    Rows are sorted ascending by report date; the latest drives the headline
    figures and the full window drives the percentile + sparkline. Raises
    ``ValueError`` if no row carries a usable date + non-commercial long/short.
    """
    parsed: list[tuple[str, int, int, int | None, int | None, int | None]] = []
    for row in rows:
        date = str(row.get("report_date_as_yyyy_mm_dd") or "")[:10]
        nc_long = _to_int(row.get("noncomm_positions_long_all"))
        nc_short = _to_int(row.get("noncomm_positions_short_all"))
        if not date or nc_long is None or nc_short is None:
            continue
        parsed.append((
            date, nc_long, nc_short,
            _to_int(row.get("comm_positions_long_all")),
            _to_int(row.get("comm_positions_short_all")),
            _to_int(row.get("open_interest_all")),
        ))
    if not parsed:
        raise ValueError("COT response had no usable row")

    parsed.sort(key=lambda t: t[0])      # oldest → newest (ISO dates sort lexically)
    date, nc_long, nc_short, c_long, c_short, oi = parsed[-1]
    net = nc_long - nc_short
    net_prev = (parsed[-2][1] - parsed[-2][2]) if len(parsed) >= 2 else None
    net_change = (net - net_prev) if net_prev is not None else None

    comm_net = (c_long - c_short) if c_long is not None and c_short is not None else None
    net_pct_oi = (net / oi * 100.0) if oi else None
    total_spec = nc_long + nc_short
    long_share = (nc_long / total_spec * 100.0) if total_spec else None

    net_history = tuple(p[1] - p[2] for p in parsed)
    history_dates = tuple(p[0] for p in parsed)
    pctile = _pct_rank(list(net_history), net)

    if pctile is None:
        extreme = 0
    elif pctile >= config.COT_EXTREME_HIGH_PCT:
        extreme = 1
    elif pctile <= config.COT_EXTREME_LOW_PCT:
        extreme = -1
    else:
        extreme = 0
    direction = 1 if net > 0 else (-1 if net < 0 else 0)

    return CotSnapshot(
        market=config.COT_GOLD_MARKET,
        report_date=date,
        noncomm_long=nc_long, noncomm_short=nc_short, net=net,
        net_prev=net_prev, net_change=net_change,
        comm_long=c_long, comm_short=c_short, comm_net=comm_net,
        open_interest=oi, net_pct_oi=net_pct_oi, long_share=long_share,
        pctile_1y=pctile, direction=direction, extreme=extreme,
        net_history=net_history, history_dates=history_dates,
        fetched_at=time.time(), generated_at=time.time(),
        stale=False, last_error=None,
    )


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class CotEngine:
    """Fetches the CFTC COT gold report, caches to disk, builds a snapshot.

    On any upstream failure the last-good snapshot is reused (flagged
    ``stale``); a restart during an outage shows it from the on-disk cache
    rather than a blank panel. The on-success snapshot is persisted so the next
    restart starts warm.
    """

    def __init__(
        self,
        cache_file: Path = config.COT_CACHE_FILE,
        timeout: float = config.COT_FETCH_TIMEOUT_SEC,
    ) -> None:
        self._cache_file = Path(cache_file)
        self._timeout = timeout
        self._cached: CotSnapshot | None = None
        self._bootstrap_from_cache()

    # ----------------------------------------------------------- compute
    def compute(self) -> CotSnapshot:
        """One refresh cycle: fetch + parse, or reuse cache flagged stale.

        Never raises — a network/parse failure returns the last-good snapshot
        (``stale=True``) so the analysis-loop worker can simply reschedule a
        retry, exactly like the real-yield path.
        """
        try:
            rows = self._fetch_rows()
            snap = parse_cot_rows(rows)
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.warning("cot: fetch/parse failed — %s", exc)
            return self._stale_from_cache(str(exc))

        self._cached = snap
        self._persist_cache(snap)
        return snap

    # -------------------------------------------------------------- HTTP
    def _fetch_rows(self) -> list[dict]:
        """GET the Legacy Futures-Only gold series (newest ``COT_HISTORY_WEEKS``)."""
        params = {
            "market_and_exchange_names": config.COT_GOLD_MARKET,
            "$select": (
                "report_date_as_yyyy_mm_dd,open_interest_all,"
                "noncomm_positions_long_all,noncomm_positions_short_all,"
                "comm_positions_long_all,comm_positions_short_all"
            ),
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": str(config.COT_HISTORY_WEEKS),
        }
        resp = requests.get(
            config.COT_SOCRATA_URL,
            params=params,
            timeout=self._timeout,
            headers={"User-Agent": config.MACRO_HTTP_USER_AGENT},
        )
        resp.raise_for_status()
        rows = resp.json()
        if not isinstance(rows, list):
            raise ValueError("COT response was not a JSON array")
        return rows

    # ------------------------------------------------------------- cache
    def _stale_from_cache(self, error: str) -> CotSnapshot:
        """Return the cached snapshot flagged stale, or an empty stale one."""
        if self._cached is not None:
            c = self._cached
            return CotSnapshot(
                market=c.market, report_date=c.report_date,
                noncomm_long=c.noncomm_long, noncomm_short=c.noncomm_short,
                net=c.net, net_prev=c.net_prev, net_change=c.net_change,
                comm_long=c.comm_long, comm_short=c.comm_short, comm_net=c.comm_net,
                open_interest=c.open_interest, net_pct_oi=c.net_pct_oi,
                long_share=c.long_share, pctile_1y=c.pctile_1y,
                direction=c.direction, extreme=c.extreme,
                net_history=c.net_history, history_dates=c.history_dates,
                fetched_at=c.fetched_at, generated_at=time.time(),
                stale=True, last_error=error,
            )
        return _empty_snapshot(error)

    def _bootstrap_from_cache(self) -> None:
        """Load the last-good snapshot so a restart shows data (flagged stale)."""
        if not self._cache_file.exists():
            return
        try:
            doc = json.loads(self._cache_file.read_text(encoding="utf-8"))
            self._cached = CotSnapshot(
                market=doc.get("market", config.COT_GOLD_MARKET),
                report_date=doc.get("report_date", ""),
                noncomm_long=doc.get("noncomm_long"),
                noncomm_short=doc.get("noncomm_short"),
                net=doc.get("net"), net_prev=doc.get("net_prev"),
                net_change=doc.get("net_change"),
                comm_long=doc.get("comm_long"), comm_short=doc.get("comm_short"),
                comm_net=doc.get("comm_net"),
                open_interest=doc.get("open_interest"),
                net_pct_oi=doc.get("net_pct_oi"), long_share=doc.get("long_share"),
                pctile_1y=doc.get("pctile_1y"), direction=int(doc.get("direction") or 0),
                extreme=int(doc.get("extreme") or 0),
                net_history=tuple(doc.get("net_history") or ()),
                history_dates=tuple(doc.get("history_dates") or ()),
                fetched_at=float(doc.get("fetched_at") or 0.0),
                generated_at=time.time(), stale=True, last_error=None,
            )
        except (OSError, ValueError, KeyError, TypeError):
            log.exception("cot: cache bootstrap failed")

    def _persist_cache(self, snap: CotSnapshot) -> None:
        """Persist the last-good snapshot to disk for a warm restart."""
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "market": snap.market, "report_date": snap.report_date,
                "noncomm_long": snap.noncomm_long, "noncomm_short": snap.noncomm_short,
                "net": snap.net, "net_prev": snap.net_prev,
                "net_change": snap.net_change,
                "comm_long": snap.comm_long, "comm_short": snap.comm_short,
                "comm_net": snap.comm_net, "open_interest": snap.open_interest,
                "net_pct_oi": snap.net_pct_oi, "long_share": snap.long_share,
                "pctile_1y": snap.pctile_1y, "direction": snap.direction,
                "extreme": snap.extreme,
                "net_history": list(snap.net_history),
                "history_dates": list(snap.history_dates),
                "fetched_at": snap.fetched_at,
            }
            self._cache_file.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            log.exception("cot: failed to write cache %s", self._cache_file)
