"""Economic calendar feed (SPEC §15).

Primary source: Forex Factory's weekly XML at
``https://nfs.faireconomy.media/ff_calendar_thisweek.xml``. The feed is
fetched on a 1-hour schedule (SPEC §15.4), parsed, filtered to high
impact events for our display currencies, and cached on disk so a quick
restart shows the last good payload immediately.

Backup source: MT5's built-in calendar via ``mt5.calendar_*`` API. We
fall back to it only when the Forex Factory fetch has failed
``CALENDAR_FAILURE_FALLBACK_AFTER`` times in a row, then automatically
resume the primary source on the first successful HTTP call.

Per SPEC §22 we *never* send a desktop notification, popup, or audio
alert for an upcoming release — the only signal is the in-UI countdown
and warning colour. The events list is the entirety of what we surface.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import requests
import xmltodict

import config

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Public dataclasses
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CalendarEvent:
    """One high-impact economic release."""

    release_ts: float        # epoch seconds (UTC)
    currency: str            # e.g. "USD", "JPY"
    title: str               # e.g. "Non-Farm Employment Change"
    impact: str              # SPEC §15.2 filter — usually "High"
    forecast: str            # raw string from the feed (may be "")
    previous: str            # raw string from the feed (may be "")
    actual: str = ""         # filled in after release; may be ""
    source: str = "forex_factory"   # or "mt5"


@dataclass(frozen=True)
class CalendarSnapshot:
    """Bundle of upcoming high-impact events."""

    generated_at: float                # epoch seconds (UTC)
    fetched_at: float                  # last successful HTTP/MT5 fetch
    source: str                        # "forex_factory" | "mt5" | "stale_cache"
    events: tuple[CalendarEvent, ...]
    last_error: str | None             # most recent fetch error, or None
    consecutive_failures: int


# --------------------------------------------------------------------------- #
# Forex Factory XML parser
# --------------------------------------------------------------------------- #

# The feed is published in US Eastern time. Python 3.9+ has zoneinfo for
# DST-aware handling without dragging in pytz.
try:
    from zoneinfo import ZoneInfo
    _EASTERN = ZoneInfo("America/New_York")
except Exception:                      # pragma: no cover — should always exist on 3.11+
    _EASTERN = timezone.utc


def _parse_ff_datetime(date_str: str, time_str: str) -> float | None:
    """Convert Forex Factory's MM-DD-YYYY + h:MMam/pm into a UTC epoch.

    Returns ``None`` for entries with no scheduled time (e.g. ``"All Day"``,
    ``"Tentative"``) — these are filtered out because a countdown does not
    apply.
    """
    if not date_str or not time_str:
        return None
    t = time_str.strip().lower()
    if t in {"all day", "tentative", "", "n/a", "day 1", "day 2"}:
        return None
    # Common formats observed in the feed.
    fmt_candidates = (
        "%m-%d-%Y %I:%M%p",  # "05-21-2026 8:30am"
        "%m-%d-%Y %I%p",     # "05-21-2026 8am"
    )
    for fmt in fmt_candidates:
        try:
            dt_naive = datetime.strptime(f"{date_str} {t}", fmt)
        except ValueError:
            continue
        dt_eastern = dt_naive.replace(tzinfo=_EASTERN)
        return dt_eastern.astimezone(timezone.utc).timestamp()
    return None


def parse_forex_factory_xml(
    body: str,
    *,
    allowed_impacts: Iterable[str] = config.CALENDAR_IMPACT_ALLOW,
    allowed_currencies: Iterable[str] = config.CALENDAR_CURRENCIES,
) -> list[CalendarEvent]:
    """Parse the Forex Factory ``weeklyevents`` XML payload.

    Filters down to ``allowed_impacts`` × ``allowed_currencies``. Events
    with unparsable times are skipped silently — the source occasionally
    publishes "All Day" or "Tentative" entries that have no countdown
    meaning for SPEC §15.3.
    """
    allowed_imp = {s.lower() for s in allowed_impacts}
    allowed_ccy = {s.upper() for s in allowed_currencies}
    try:
        doc = xmltodict.parse(body)
    except Exception as exc:           # noqa: BLE001 — caller wants graceful failure
        raise ValueError(f"malformed Forex Factory XML: {exc}") from exc

    raw_events = (doc.get("weeklyevents") or {}).get("event") or []
    if isinstance(raw_events, dict):
        raw_events = [raw_events]

    out: list[CalendarEvent] = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        impact = (raw.get("impact") or "").strip()
        currency = (raw.get("country") or "").strip().upper()
        if impact.lower() not in allowed_imp:
            continue
        if currency not in allowed_ccy:
            continue
        release_ts = _parse_ff_datetime(
            (raw.get("date") or "").strip(),
            (raw.get("time") or "").strip(),
        )
        if release_ts is None:
            continue
        out.append(CalendarEvent(
            release_ts=release_ts,
            currency=currency,
            title=(raw.get("title") or "").strip(),
            impact=impact,
            forecast=(raw.get("forecast") or "").strip(),
            previous=(raw.get("previous") or "").strip(),
            actual=(raw.get("actual") or "").strip(),
            source="forex_factory",
        ))
    out.sort(key=lambda e: e.release_ts)
    return out


# --------------------------------------------------------------------------- #
# MT5 calendar fallback
# --------------------------------------------------------------------------- #

def parse_mt5_calendar(window_days: int = 7) -> list[CalendarEvent]:
    """Pull SPEC §15.1 backup data from MT5's built-in calendar.

    Older brokers / MT5 builds do not implement ``calendar_*``; the
    function then returns an empty list rather than raising so the
    analysis loop survives.
    """
    try:
        import MetaTrader5 as mt5  # local import — only needed on fallback
    except Exception:                # pragma: no cover
        return []
    fns = ("calendar_country_get", "calendar_event_get",
           "calendar_value_history")
    if not all(hasattr(mt5, f) for f in fns):
        log.info("calendar: MT5 build lacks calendar_* API, fallback unavailable")
        return []
    now = time.time()
    to_dt = datetime.fromtimestamp(now + window_days * 86400.0, tz=timezone.utc)
    from_dt = datetime.fromtimestamp(now - 3600.0, tz=timezone.utc)
    try:
        raw_values = mt5.calendar_value_history(from_dt, to_dt) or ()
        events_by_id = {e.id: e for e in (mt5.calendar_event_get() or ())}
        countries_by_id = {c.id: c for c in (mt5.calendar_country_get() or ())}
    except Exception:                # noqa: BLE001 — broker-specific quirks
        log.exception("calendar: MT5 fallback call failed")
        return []

    allowed_imp = {s.lower() for s in config.CALENDAR_IMPACT_ALLOW}
    allowed_ccy = {s.upper() for s in config.CALENDAR_CURRENCIES}
    out: list[CalendarEvent] = []
    for v in raw_values:
        ev = events_by_id.get(getattr(v, "event_id", 0))
        if ev is None:
            continue
        ctry = countries_by_id.get(getattr(ev, "country_id", 0))
        if ctry is None:
            continue
        currency = (getattr(ctry, "currency", "") or "").upper()
        if currency not in allowed_ccy:
            continue
        impact = _mt5_importance_to_label(getattr(ev, "importance", 0))
        if impact.lower() not in allowed_imp:
            continue
        release_ts = float(getattr(v, "time", 0))
        if release_ts <= 0:
            continue
        out.append(CalendarEvent(
            release_ts=release_ts,
            currency=currency,
            title=getattr(ev, "name", "") or "",
            impact=impact,
            forecast=_mt5_value_str(getattr(v, "forecast_value", None)),
            previous=_mt5_value_str(getattr(v, "prev_value", None)),
            actual=_mt5_value_str(getattr(v, "actual_value", None)),
            source="mt5",
        ))
    out.sort(key=lambda e: e.release_ts)
    return out


def _mt5_importance_to_label(level: int) -> str:
    """Map MT5's ``ENUM_CALENDAR_EVENT_IMPORTANCE`` to the SPEC §15.2 label."""
    return {1: "Low", 2: "Medium", 3: "High"}.get(int(level), "Low")


def _mt5_value_str(v: float | int | str | None) -> str:
    """Format an MT5 calendar value field for display.

    MT5 may return ``None`` for missing data, NaN for "no forecast",
    numeric strings, or already-formatted text — we normalise everything
    to the same string convention used by the Forex Factory feed.
    """
    if v is None:
        return ""
    if isinstance(v, float):
        if v != v:               # NaN
            return ""
        return f"{v:g}"
    return str(v)


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class CalendarEngine:
    """Pulls + parses + caches the SPEC §15 calendar with auto-fallback."""

    def __init__(
        self,
        url: str = config.CALENDAR_FF_URL,
        cache_file: Path = config.CALENDAR_CACHE_FILE,
        timeout: float = config.CALENDAR_FETCH_TIMEOUT_SEC,
        retries: int = config.CALENDAR_FETCH_RETRIES,
        failure_fallback_after: int = config.CALENDAR_FAILURE_FALLBACK_AFTER,
    ) -> None:
        self._url = url
        self._cache_file = Path(cache_file)
        self._timeout = timeout
        self._retries = retries
        self._failure_fallback_after = failure_fallback_after
        self._consecutive_failures = 0
        self._last_fetch_ok = 0.0
        self._last_error: str | None = None
        self._last_events: tuple[CalendarEvent, ...] = ()
        self._last_source = "stale_cache"
        self._bootstrap_from_cache()

    # ------------------------------------------------------- bootstrap
    def _bootstrap_from_cache(self) -> None:
        if not self._cache_file.exists():
            return
        try:
            body = self._cache_file.read_text(encoding="utf-8")
            events = parse_forex_factory_xml(body)
        except Exception:                # noqa: BLE001 — corrupted cache
            log.exception("calendar: cache bootstrap failed")
            return
        self._last_events = tuple(events)
        self._last_source = "stale_cache"
        # Use the cache file's mtime so the UI reports an honest "fetched at"
        # for cached data instead of epoch zero (1970).
        try:
            self._last_fetch_ok = float(self._cache_file.stat().st_mtime)
        except OSError:
            self._last_fetch_ok = 0.0
        log.info("calendar: bootstrapped %d events from cache (mtime=%s)",
                 len(events), self._last_fetch_ok)

    # --------------------------------------------------------- compute
    def compute(self) -> CalendarSnapshot:
        """One refresh cycle. Tries Forex Factory then falls back to MT5.

        Returns the latest snapshot. All ``_last_*`` field mutation happens
        in this method so the state-transition graph stays in one place.
        """
        body = self._http_fetch()
        if body is not None:
            try:
                events = parse_forex_factory_xml(body)
            except ValueError as exc:
                self._record_failure(str(exc))
                events = self._select_fallback_events()
                if events:
                    self._last_events = tuple(events)
                    self._last_source = "mt5"
            else:
                self._consecutive_failures = 0
                self._last_error = None
                self._last_fetch_ok = time.time()
                self._last_events = tuple(events)
                self._last_source = "forex_factory"
                self._store_cache(body)
        else:
            events = self._select_fallback_events()
            if events:
                self._last_events = tuple(events)
                self._last_source = "mt5"

        return CalendarSnapshot(
            generated_at=time.time(),
            fetched_at=self._last_fetch_ok,
            source=self._last_source,
            events=tuple(self._last_events),
            last_error=self._last_error,
            consecutive_failures=self._consecutive_failures,
        )

    # ------------------------------------------------------------ HTTP
    def _http_fetch(self) -> str | None:
        """Fetch the XML body or return None and record the failure cause."""
        last_exc: Exception | None = None
        for attempt in range(1, self._retries + 1):
            try:
                resp = requests.get(self._url, timeout=self._timeout)
                resp.raise_for_status()
                return resp.text
            except requests.RequestException as exc:
                last_exc = exc
                # Per-retry detail is DEBUG noise — the overall failure
                # is bubbled up to WARNING via _record_failure below.
                log.debug("calendar: HTTP attempt %d/%d failed: %s",
                          attempt, self._retries, exc)
        if last_exc is not None:
            self._record_failure(f"http: {last_exc}")
        return None

    def _record_failure(self, msg: str) -> None:
        self._consecutive_failures += 1
        self._last_error = msg
        log.warning("calendar: failure #%d — %s", self._consecutive_failures, msg)

    def _select_fallback_events(self) -> list[CalendarEvent]:
        """SPEC §15.1 backup: pull events from MT5 once HTTP has failed enough.

        Pure-ish: returns events from MT5 if the failure threshold is met,
        otherwise an empty list. State (``_last_events`` / ``_last_source``)
        is intentionally not mutated here — :meth:`compute` owns those
        writes so the transition graph is in one place.
        """
        if self._consecutive_failures < self._failure_fallback_after:
            return []
        try:
            events = parse_mt5_calendar()
        except Exception:                # noqa: BLE001 — never let it bubble
            log.exception("calendar: MT5 fallback raised")
            return []
        if events:
            log.info("calendar: MT5 fallback supplied %d events", len(events))
        return events

    def _store_cache(self, body: str) -> None:
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            self._cache_file.write_text(body, encoding="utf-8")
        except OSError:                  # noqa: BLE001 — cache is best-effort
            log.exception("calendar: failed to write cache %s", self._cache_file)
