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

def test_parse_ff_datetime_eastern_to_utc():
    # 05-21-2026 8:30am EDT (UTC-4 in May) → 12:30 UTC.
    ts = _parse_ff_datetime("05-21-2026", "8:30am")
    assert ts is not None
    # Allow ±1s of slack to absorb tzdata details across environments.
    from datetime import datetime, timezone
    expected = datetime(2026, 5, 21, 12, 30, tzinfo=timezone.utc).timestamp()
    assert abs(ts - expected) < 1


def test_parse_ff_datetime_no_minutes():
    ts = _parse_ff_datetime("05-21-2026", "8am")
    assert ts is not None
    from datetime import datetime, timezone
    expected = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc).timestamp()
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
    eng = CalendarEngine(url="http://x/", cache_file=cache, retries=1)
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
    eng = CalendarEngine(url="http://x/", cache_file=cache, retries=2,
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
    eng = CalendarEngine(url="http://x/", cache_file=cache,
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
    assert len(snap2.events) == 1
    assert snap2.events[0].source == "mt5"


def test_engine_bootstraps_from_existing_cache(tmp_path, mocker):
    cache = tmp_path / "thisweek.xml"
    cache.write_text(SAMPLE_XML, encoding="utf-8")
    # No HTTP call made; bootstrap reads the cache directly.
    mocker.patch("analyzer.calendar_feed.requests.get",
                 side_effect=AssertionError("no HTTP expected"))
    eng = CalendarEngine(url="http://x/", cache_file=cache, retries=0)
    # bootstrap fills _last_events; without compute() the snapshot would be
    # populated only on the first refresh. Verify the internal state directly.
    assert len(eng._last_events) >= 2
    assert eng._last_source == "stale_cache"


def test_engine_corrupt_cache_does_not_crash(tmp_path):
    cache = tmp_path / "thisweek.xml"
    cache.write_text("<<<not xml", encoding="utf-8")
    eng = CalendarEngine(url="http://x/", cache_file=cache, retries=0)
    # Bootstrap silently logs; _last_events remains empty.
    assert eng._last_events == ()
