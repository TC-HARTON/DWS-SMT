"""Unit tests for analyzer.calendar_feed (SPEC §15)."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

import config
from analyzer.calendar_feed import (
    CalendarEngine,
    _parse_ff_datetime,
    parse_forex_factory_xml,
)


SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<weeklyevents>
  <event>
    <title>Non-Farm Employment Change</title>
    <country>USD</country>
    <date>05-21-2026</date>
    <time>8:30am</time>
    <impact>High</impact>
    <forecast>185K</forecast>
    <previous>175K</previous>
  </event>
  <event>
    <title>BOJ Press Conference</title>
    <country>JPY</country>
    <date>05-21-2026</date>
    <time>2:00pm</time>
    <impact>High</impact>
    <forecast/>
    <previous/>
  </event>
  <event>
    <title>CPI m/m</title>
    <country>EUR</country>
    <date>05-22-2026</date>
    <time>10:00am</time>
    <impact>High</impact>
    <forecast>0.3%</forecast>
    <previous>0.2%</previous>
  </event>
  <event>
    <title>Tentative G7 Statement</title>
    <country>USD</country>
    <date>05-22-2026</date>
    <time>All Day</time>
    <impact>High</impact>
  </event>
  <event>
    <title>OPEC Crude Oil Inventories</title>
    <country>XYZ</country>
    <date>05-22-2026</date>
    <time>11:00am</time>
    <impact>High</impact>
  </event>
</weeklyevents>
"""


# --------------------------------------------------------------------------- #
# datetime parser
# --------------------------------------------------------------------------- #

def test_parse_ff_datetime_is_utc_not_eastern():
    # The faireconomy feed is GMT/UTC: "8:30am" means 08:30 UTC (NOT 8:30 ET).
    # Regression guard for the +4/5h bug that put NFP at 01:30 JST.
    ts = _parse_ff_datetime("05-21-2026", "8:30am")
    assert ts is not None
    from datetime import datetime, timezone
    expected = datetime(2026, 5, 21, 8, 30, tzinfo=timezone.utc).timestamp()
    assert abs(ts - expected) < 1


def test_parse_ff_datetime_nfp_lands_at_2130_jst():
    # Real-world check: NFP is published "12:30pm" in the GMT feed (= 8:30 ET).
    # That must be 12:30 UTC → 21:30 JST, never 01:30 JST.
    from datetime import datetime, timezone, timedelta
    ts = _parse_ff_datetime("06-05-2026", "12:30pm")
    jst = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=9)))
    assert (jst.hour, jst.minute) == (21, 30)


def test_parse_ff_datetime_no_minutes():
    ts = _parse_ff_datetime("05-21-2026", "8am")
    assert ts is not None
    from datetime import datetime, timezone
    expected = datetime(2026, 5, 21, 8, 0, tzinfo=timezone.utc).timestamp()
    assert abs(ts - expected) < 1


@pytest.mark.parametrize("t", ["All Day", "Tentative", "", "n/a", "Day 1"])
def test_parse_ff_datetime_unparsable_returns_none(t):
    assert _parse_ff_datetime("05-21-2026", t) is None


def test_parse_ff_datetime_missing_inputs():
    assert _parse_ff_datetime("", "8:30am") is None
    assert _parse_ff_datetime("05-21-2026", "") is None


# --------------------------------------------------------------------------- #
# XML parser
# --------------------------------------------------------------------------- #

def test_parse_xml_keeps_high_impact_only():
    """SPEC §15.2: Low / Medium 完全非表示."""
    events = parse_forex_factory_xml(SAMPLE_XML)
    impacts = {e.impact for e in events}
    assert impacts == {"High"}


def test_parse_xml_drops_non_display_currencies():
    """XYZ is not in CALENDAR_CURRENCIES (= FIAT_CURRENCIES)."""
    events = parse_forex_factory_xml(SAMPLE_XML)
    currencies = {e.currency for e in events}
    assert "XYZ" not in currencies
    assert {"USD", "JPY"}.issubset(currencies)


def test_parse_xml_drops_non_rate_employment_events():
    """SPEC §15: only rate-decision / employment events surface — a High-impact
    CPI release is dropped by the title-keyword filter."""
    events = parse_forex_factory_xml(SAMPLE_XML)
    titles = [e.title for e in events]
    assert "CPI m/m" not in titles
    assert "Non-Farm Employment Change" in titles   # employment keyword
    assert "BOJ Press Conference" in titles         # rate-decision keyword


def test_parse_xml_drops_unparsable_time_events():
    """'All Day' / Tentative events are filtered out."""
    events = parse_forex_factory_xml(SAMPLE_XML)
    titles = [e.title for e in events]
    assert "Tentative G7 Statement" not in titles


def test_parse_xml_sorted_by_release_time():
    events = parse_forex_factory_xml(SAMPLE_XML)
    timestamps = [e.release_ts for e in events]
    assert timestamps == sorted(timestamps)


def test_parse_xml_invalid_input_raises_valueerror():
    with pytest.raises(ValueError):
        parse_forex_factory_xml("not xml at all <<<")


def test_parse_xml_captures_source_url():
    xml = (
        '<?xml version="1.0"?><weeklyevents><event>'
        '<title>Non-Farm Employment Change</title><country>USD</country>'
        '<date>06-05-2026</date><time>12:30pm</time><impact>High</impact>'
        '<url>https://www.forexfactory.com/calendar/123-usd-nfp</url>'
        '</event></weeklyevents>'
    )
    events = parse_forex_factory_xml(xml)
    assert len(events) == 1
    assert events[0].source_url == "https://www.forexfactory.com/calendar/123-usd-nfp"


def test_adp_is_not_labelled_as_official_nfp():
    # ADP is a PRIVATE payroll estimate, not the BLS NFP — distinct label.
    from dashboard.serialize import _jp_calendar_title
    assert _jp_calendar_title("ADP Non-Farm Employment Change", "USD") == "ADP雇用統計"
    assert _jp_calendar_title("Non-Farm Employment Change", "USD") == "米雇用統計 (NFP)"


def test_cb_events_carry_source_url():
    from analyzer.calendar_feed import upcoming_cb_events, _FRANKFURT
    evs = upcoming_cb_events(
        0.0, currency="EUR", dates=("2030-06-05",), hour=14, minute=15,
        tz=_FRANKFURT, title="ECB", source_url="https://www.ecb.europa.eu/x")
    assert evs and evs[0].source_url == "https://www.ecb.europa.eu/x"


def test_parse_xml_empty_payload_returns_empty_list():
    out = parse_forex_factory_xml(
        '<?xml version="1.0"?><weeklyevents></weeklyevents>'
    )
    assert out == []


def test_parse_xml_handles_single_event_dict():
    """xmltodict collapses a single child element to a dict, not a list."""
    body = """<?xml version="1.0"?>
    <weeklyevents>
      <event>
        <title>FOMC Statement</title>
        <country>USD</country>
        <date>05-21-2026</date>
        <time>2:00pm</time>
        <impact>High</impact>
      </event>
    </weeklyevents>"""
    events = parse_forex_factory_xml(body)
    assert len(events) == 1
    assert events[0].currency == "USD"


# --------------------------------------------------------------------------- #
# CalendarEngine — HTTP success + cache write
# --------------------------------------------------------------------------- #

def _stub_response(text: str, status: int = 200):
    r = MagicMock()
    r.text = text
    r.status_code = status
    r.raise_for_status.side_effect = (
        None if status < 400 else requests.HTTPError(f"HTTP {status}")
    )
    return r


def test_engine_compute_success_writes_cache(tmp_path: Path, mocker):
    cache = tmp_path / "thisweek.xml"
    eng = CalendarEngine(urls=("http://x/",), cache_file=cache, retries=1)
    mocker.patch("analyzer.calendar_feed.requests.get",
                 return_value=_stub_response(SAMPLE_XML))
    snap = eng.compute()
    assert snap.source == "forex_factory"
    assert snap.consecutive_failures == 0
    assert snap.last_error is None
    assert len(snap.events) >= 2
    assert cache.exists()
    assert cache.read_text(encoding="utf-8") == SAMPLE_XML


def test_engine_compute_http_failure_keeps_previous_events(tmp_path, mocker):
    cache = tmp_path / "thisweek.xml"
    eng = CalendarEngine(urls=("http://x/",), cache_file=cache, retries=2,
                         failure_fallback_after=5)
    # First success seeds the cache.
    mocker.patch("analyzer.calendar_feed.requests.get",
                 return_value=_stub_response(SAMPLE_XML))
    snap_ok = eng.compute()
    assert snap_ok.source == "forex_factory"

    # Now break HTTP. Two failures, still below the fallback threshold (5).
    mocker.patch("analyzer.calendar_feed.requests.get",
                 side_effect=requests.ConnectionError("offline"))
    snap_fail = eng.compute()
    assert snap_fail.consecutive_failures >= 1
    assert snap_fail.last_error is not None
    # Previous events are kept.
    assert len(snap_fail.events) == len(snap_ok.events)


def test_engine_falls_back_to_mt5_after_threshold(tmp_path, mocker):
    cache = tmp_path / "thisweek.xml"
    eng = CalendarEngine(urls=("http://x/",), cache_file=cache,
                         retries=1, failure_fallback_after=2)
    mocker.patch("analyzer.calendar_feed.requests.get",
                 side_effect=requests.ConnectionError("offline"))

    # Stub MT5 calendar with a single high-impact USD event.
    def fake_mt5_calendar(window_days=7):
        from analyzer.calendar_feed import CalendarEvent
        return [CalendarEvent(
            release_ts=time.time() + 3600,
            currency="USD", title="MT5 stub", impact="High",
            forecast="", previous="", source="mt5",
        )]
    mocker.patch("analyzer.calendar_feed.parse_mt5_calendar",
                 side_effect=fake_mt5_calendar)

    # First failure — still below threshold (no fallback yet).
    snap1 = eng.compute()
    assert snap1.source != "mt5"
    # Second failure — threshold reached, fallback should fire.
    snap2 = eng.compute()
    assert snap2.source == "mt5"
    # The MT5 event is present (alongside the always-appended scheduled
    # FOMC/NFP forward events).
    mt5_events = [e for e in snap2.events if e.source == "mt5"]
    assert len(mt5_events) == 1
    assert mt5_events[0].title == "MT5 stub"


def test_engine_bootstraps_from_existing_cache(tmp_path, mocker):
    cache = tmp_path / "thisweek.xml"
    cache.write_text(SAMPLE_XML, encoding="utf-8")
    # No HTTP call made; bootstrap reads the cache directly.
    mocker.patch("analyzer.calendar_feed.requests.get",
                 side_effect=AssertionError("no HTTP expected"))
    eng = CalendarEngine(urls=("http://x/",), cache_file=cache, retries=0)
    # bootstrap fills _last_events; without compute() the snapshot would be
    # populated only on the first refresh. Verify the internal state directly.
    assert len(eng._last_events) >= 2
    assert eng._last_source == "stale_cache"


def test_dedupe_events_drops_duplicates():
    from analyzer.calendar_feed import CalendarEvent, _dedupe_events
    a = CalendarEvent(release_ts=100.0, currency="USD", title="FOMC Statement",
                      impact="High", forecast="", previous="")
    b = CalendarEvent(release_ts=100.0, currency="USD", title="FOMC Statement",
                      impact="High", forecast="", previous="")   # dup of a
    c = CalendarEvent(release_ts=50.0, currency="JPY", title="BOJ Press Conference",
                      impact="High", forecast="", previous="")
    out = _dedupe_events([a, b, c])
    assert len(out) == 2                       # the duplicate is dropped
    assert [e.release_ts for e in out] == [50.0, 100.0]   # sorted by time


def test_engine_fetches_and_merges_multiple_feeds(tmp_path, mocker):
    """This week + next week are both fetched; overlapping events de-duplicate."""
    cache = tmp_path / "thisweek.xml"
    eng = CalendarEngine(urls=("http://a/", "http://b/"),
                         cache_file=cache, retries=1)
    mocker.patch("analyzer.calendar_feed.requests.get",
                 return_value=_stub_response(SAMPLE_XML))
    snap = eng.compute()
    # Both feeds returned the same XML → Forex Factory events merge but
    # de-duplicate (2, not 4); scheduled forward events are appended on top.
    assert snap.source == "forex_factory"
    ff = [e for e in snap.events if e.source == "forex_factory"]
    assert len(ff) == 2
    assert cache.exists()                      # primary feed cached


def test_upcoming_fomc_events_returns_future_only():
    from analyzer.calendar_feed import upcoming_fomc_events
    out = upcoming_fomc_events(now_ts=0.0, count=3)        # now before all dates
    assert len(out) == 3
    assert all(e.source == "scheduled" for e in out)
    assert all(e.currency == "USD" and "FOMC" in e.title for e in out)
    assert [e.release_ts for e in out] == sorted(e.release_ts for e in out)
    assert upcoming_fomc_events(now_ts=4e9, count=3) == []  # now after all dates


def test_upcoming_fomc_events_skip_dates():
    from analyzer.calendar_feed import upcoming_fomc_events
    from datetime import datetime, timezone
    first = config.FOMC_MEETING_DATES[0]
    out = upcoming_fomc_events(now_ts=0.0, count=3,
                               skip_dates=frozenset({first}))
    days = {datetime.fromtimestamp(e.release_ts, tz=timezone.utc)
            .strftime("%Y-%m-%d") for e in out}
    assert first not in days


def test_fetch_upcoming_nfp_events(mocker):
    from analyzer.calendar_feed import fetch_upcoming_nfp_events
    if not config.FRED_API_KEY:
        assert fetch_upcoming_nfp_events(now_ts=0.0) == []   # no key → []
        return
    resp = MagicMock()
    resp.json.return_value = {"release_dates": [
        {"date": "2026-06-05"}, {"date": "2026-07-02"}, {"date": "2026-08-07"}]}
    resp.raise_for_status.return_value = None
    mocker.patch("analyzer.calendar_feed.requests.get", return_value=resp)
    out = fetch_upcoming_nfp_events(now_ts=0.0, count=2)
    assert len(out) == 2
    assert all(e.source == "scheduled" and e.currency == "USD" for e in out)
    assert all("Payroll" in e.title for e in out)


def test_fetch_upcoming_nfp_events_http_failure_returns_empty(mocker):
    from analyzer.calendar_feed import fetch_upcoming_nfp_events
    if not config.FRED_API_KEY:
        return
    mocker.patch("analyzer.calendar_feed.requests.get",
                 side_effect=requests.ConnectionError("offline"))
    assert fetch_upcoming_nfp_events(now_ts=0.0) == []


def test_engine_corrupt_cache_does_not_crash(tmp_path):
    cache = tmp_path / "thisweek.xml"
    cache.write_text("<<<not xml", encoding="utf-8")
    eng = CalendarEngine(urls=("http://x/",), cache_file=cache, retries=0)
    # Bootstrap silently logs; _last_events remains empty.
    assert eng._last_events == ()


# --------------------------------------------------------------------------- #
# Rate-limit hardening: faireconomy serves a 200 HTML "Rate Limited" page when
# throttled. It must be treated as a fetch failure, never parsed as 0 events
# (which would wipe the live list and poison the cache with HTML).
# --------------------------------------------------------------------------- #

RATE_LIMITED_HTML = (
    "<!DOCTYPE html><html><head><title>Rate Limited</title></head>"
    "<body>Rate limit exceeded, please slow down.</body></html>"
)


def test_http_fetch_rejects_rate_limit_html(tmp_path, mocker):
    eng = CalendarEngine(urls=("http://x/",), cache_file=tmp_path / "c.xml",
                         retries=1)
    mocker.patch("analyzer.calendar_feed.requests.get",
                 return_value=_stub_response(RATE_LIMITED_HTML))
    # A 200 that is not the XML feed must be rejected (return None = failure).
    assert eng._http_fetch("http://x/") is None


def test_compute_rate_limited_keeps_events_and_protects_cache(tmp_path, mocker):
    cache = tmp_path / "thisweek.xml"
    eng = CalendarEngine(urls=("http://x/",), cache_file=cache, retries=1,
                         failure_fallback_after=5)
    # Seed a good fetch (events + cache).
    mocker.patch("analyzer.calendar_feed.requests.get",
                 return_value=_stub_response(SAMPLE_XML))
    ok = eng.compute()
    ff_before = [e for e in ok.events if e.source == "forex_factory"]
    assert ff_before and cache.read_text(encoding="utf-8") == SAMPLE_XML

    # Now the rate-limit 200-HTML page.
    mocker.patch("analyzer.calendar_feed.requests.get",
                 return_value=_stub_response(RATE_LIMITED_HTML))
    rl = eng.compute()
    # Treated as a failure: previous FF events retained, cache NOT overwritten.
    assert rl.consecutive_failures >= 1
    ff_after = [e for e in rl.events if e.source == "forex_factory"]
    assert len(ff_after) == len(ff_before)
    assert cache.read_text(encoding="utf-8") == SAMPLE_XML   # not poisoned
