"""Sprint 23 — calendar fetch + parse + RRULE expansion tests.

All tests use hand-rolled ICS bytes — no network. Coverage:

  * Single-event parsing (DTSTART/DTEND/SUMMARY/LOCATION/etc)
  * All-day events (DATE rather than DATETIME)
  * Recurring events: DAILY / WEEKLY / MONTHLY with COUNT or UNTIL
  * Window filtering — events outside the window are excluded
  * Helpers: from_settings configuration errors, secrets missing, network failure
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_core.settings import AgentSettings
from agent_core.work.calendar import (
    CalendarEvent,
    CalendarFetchError,
    CalendarFetcher,
    _coerce_dt,
    _overlaps,
    _parse_and_expand,
    _strip_mailto,
    fetch_today,
    fetch_window,
)


UTC = timezone.utc


# ── Hand-rolled ICS fixtures ───────────────────────────────────────────────


def _ics(*events: str) -> bytes:
    """Wrap one or more VEVENT blocks in a minimal valid ICS envelope."""
    body = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//test//test//EN\r\n"
        + "".join(events)
        + "END:VCALENDAR\r\n"
    )
    return body.encode()


def _vevent_single() -> str:
    return (
        "BEGIN:VEVENT\r\n"
        "UID:single-1@test\r\n"
        "DTSTART:20260504T140000Z\r\n"
        "DTEND:20260504T150000Z\r\n"
        "SUMMARY:Q2 review\r\n"
        "LOCATION:Zoom\r\n"
        "DESCRIPTION:Review the Q2 budget with the team\r\n"
        "ORGANIZER:mailto:boss@example.com\r\n"
        "ATTENDEE:mailto:aj@example.com\r\n"
        "ATTENDEE:mailto:charlotte@example.com\r\n"
        "END:VEVENT\r\n"
    )


def _vevent_all_day() -> str:
    return (
        "BEGIN:VEVENT\r\n"
        "UID:allday-1@test\r\n"
        "DTSTART;VALUE=DATE:20260504\r\n"
        "DTEND;VALUE=DATE:20260505\r\n"
        "SUMMARY:Off site day\r\n"
        "END:VEVENT\r\n"
    )


def _vevent_recurring_daily(*, count: int = 5) -> str:
    return (
        "BEGIN:VEVENT\r\n"
        "UID:daily-1@test\r\n"
        "DTSTART:20260504T100000Z\r\n"
        "DTEND:20260504T103000Z\r\n"
        "SUMMARY:Daily standup\r\n"
        f"RRULE:FREQ=DAILY;COUNT={count}\r\n"
        "END:VEVENT\r\n"
    )


def _vevent_recurring_weekly_until() -> str:
    return (
        "BEGIN:VEVENT\r\n"
        "UID:weekly-1@test\r\n"
        "DTSTART:20260504T160000Z\r\n"
        "DTEND:20260504T170000Z\r\n"
        "SUMMARY:Weekly 1:1\r\n"
        "RRULE:FREQ=WEEKLY;UNTIL=20260601T000000Z\r\n"
        "END:VEVENT\r\n"
    )


# ── _parse_and_expand: single events ───────────────────────────────────────


def test_parse_single_event_extracts_basic_fields():
    raw = _ics(_vevent_single())
    start = datetime(2026, 5, 4, 0, 0, tzinfo=UTC)
    end = start + timedelta(days=1)
    events = _parse_and_expand(raw, start=start, end=end)

    assert len(events) == 1
    e = events[0]
    assert e.summary == "Q2 review"
    assert e.location == "Zoom"
    assert e.organizer == "boss@example.com"
    assert "aj@example.com" in e.attendees
    assert "charlotte@example.com" in e.attendees
    assert e.start == datetime(2026, 5, 4, 14, 0, tzinfo=UTC)
    assert e.end == datetime(2026, 5, 4, 15, 0, tzinfo=UTC)
    assert e.all_day is False


def test_parse_single_event_excluded_when_outside_window():
    raw = _ics(_vevent_single())
    # Window = May 5 (the day after the event)
    start = datetime(2026, 5, 5, 0, 0, tzinfo=UTC)
    end = start + timedelta(days=1)
    events = _parse_and_expand(raw, start=start, end=end)
    assert events == []


def test_parse_all_day_event_marked_correctly():
    raw = _ics(_vevent_all_day())
    start = datetime(2026, 5, 4, 0, 0, tzinfo=UTC)
    end = datetime(2026, 5, 5, 0, 0, tzinfo=UTC)
    events = _parse_and_expand(raw, start=start, end=end)
    assert len(events) == 1
    assert events[0].all_day is True
    assert events[0].summary == "Off site day"


# ── Recurring expansion ────────────────────────────────────────────────────


def test_parse_daily_recurring_expands_within_window():
    """DAILY;COUNT=5 starting May 4 → 5 instances on May 4-8."""
    raw = _ics(_vevent_recurring_daily(count=5))
    start = datetime(2026, 5, 4, 0, 0, tzinfo=UTC)
    end = datetime(2026, 5, 9, 0, 0, tzinfo=UTC)  # May 4-8 inclusive
    events = _parse_and_expand(raw, start=start, end=end)
    assert len(events) == 5
    summaries = {e.summary for e in events}
    assert summaries == {"Daily standup"}
    starts = sorted(e.start.date() for e in events)
    assert starts[0].isoformat() == "2026-05-04"
    assert starts[-1].isoformat() == "2026-05-08"


def test_parse_daily_recurring_respects_count_limit():
    raw = _ics(_vevent_recurring_daily(count=3))
    start = datetime(2026, 5, 4, 0, 0, tzinfo=UTC)
    end = datetime(2026, 5, 30, 0, 0, tzinfo=UTC)
    events = _parse_and_expand(raw, start=start, end=end)
    # COUNT=3 caps the expansion regardless of window
    assert len(events) == 3


def test_parse_weekly_recurring_until_terminates_correctly():
    """WEEKLY;UNTIL=20260601 starting May 4 → instances on May 4, 11, 18, 25."""
    raw = _ics(_vevent_recurring_weekly_until())
    start = datetime(2026, 5, 4, 0, 0, tzinfo=UTC)
    end = datetime(2026, 6, 5, 0, 0, tzinfo=UTC)
    events = _parse_and_expand(raw, start=start, end=end)
    # Instances within window: May 4, 11, 18, 25 (June 1 falls outside UNTIL)
    starts = sorted(e.start.date().isoformat() for e in events)
    assert starts == ["2026-05-04", "2026-05-11", "2026-05-18", "2026-05-25"]


def test_parse_weekly_window_clips_recurring():
    """Only the May 11 instance falls in a tight window."""
    raw = _ics(_vevent_recurring_weekly_until())
    start = datetime(2026, 5, 11, 0, 0, tzinfo=UTC)
    end = datetime(2026, 5, 12, 0, 0, tzinfo=UTC)
    events = _parse_and_expand(raw, start=start, end=end)
    assert len(events) == 1
    assert events[0].start.date().isoformat() == "2026-05-11"


def test_parse_mixed_single_and_recurring():
    raw = _ics(_vevent_single(), _vevent_recurring_daily(count=2))
    start = datetime(2026, 5, 4, 0, 0, tzinfo=UTC)
    end = datetime(2026, 5, 6, 0, 0, tzinfo=UTC)
    events = _parse_and_expand(raw, start=start, end=end)
    summaries = sorted(e.summary for e in events)
    assert summaries == ["Daily standup", "Daily standup", "Q2 review"]


def test_parse_returns_events_sorted_by_start():
    raw = _ics(_vevent_recurring_daily(count=3), _vevent_single())
    start = datetime(2026, 5, 4, 0, 0, tzinfo=UTC)
    end = datetime(2026, 5, 7, 0, 0, tzinfo=UTC)
    events = _parse_and_expand(raw, start=start, end=end)
    starts = [e.start for e in events]
    assert starts == sorted(starts)


# ── Edge cases ─────────────────────────────────────────────────────────────


def test_parse_event_without_dtend_treated_as_zero_length():
    raw = _ics(
        "BEGIN:VEVENT\r\n"
        "UID:no-end@test\r\n"
        "DTSTART:20260504T100000Z\r\n"
        "SUMMARY:Marker\r\n"
        "END:VEVENT\r\n"
    )
    start = datetime(2026, 5, 4, 0, 0, tzinfo=UTC)
    end = datetime(2026, 5, 5, 0, 0, tzinfo=UTC)
    events = _parse_and_expand(raw, start=start, end=end)
    assert len(events) == 1
    assert events[0].start == events[0].end


def test_parse_skips_malformed_vevent_block():
    """A VEVENT with no DTSTART should be skipped, not crash the whole feed."""
    raw = _ics(
        "BEGIN:VEVENT\r\nUID:bad@test\r\nSUMMARY:no start\r\nEND:VEVENT\r\n",
        _vevent_single(),
    )
    start = datetime(2026, 5, 4, 0, 0, tzinfo=UTC)
    end = datetime(2026, 5, 5, 0, 0, tzinfo=UTC)
    events = _parse_and_expand(raw, start=start, end=end)
    assert len(events) == 1
    assert events[0].summary == "Q2 review"


def test_parse_empty_calendar_returns_empty_list():
    raw = _ics()
    events = _parse_and_expand(
        raw,
        start=datetime(2026, 5, 4, tzinfo=UTC),
        end=datetime(2026, 5, 5, tzinfo=UTC),
    )
    assert events == []


# ── Helpers ────────────────────────────────────────────────────────────────


def test_strip_mailto_removes_prefix():
    assert _strip_mailto("mailto:a@b.com") == "a@b.com"


def test_strip_mailto_handles_uppercase():
    assert _strip_mailto("MAILTO:a@b.com") == "a@b.com"


def test_strip_mailto_passes_through_plain_value():
    assert _strip_mailto("a@b.com") == "a@b.com"


def test_overlaps_strict_inclusive_at_start():
    a_s = datetime(2026, 5, 4, 9, tzinfo=UTC)
    a_e = datetime(2026, 5, 4, 10, tzinfo=UTC)
    b_s = datetime(2026, 5, 4, 9, tzinfo=UTC)
    b_e = datetime(2026, 5, 4, 11, tzinfo=UTC)
    assert _overlaps(a_s, a_e, b_s, b_e) is True


def test_overlaps_returns_false_when_disjoint():
    a_s = datetime(2026, 5, 4, 9, tzinfo=UTC)
    a_e = datetime(2026, 5, 4, 10, tzinfo=UTC)
    b_s = datetime(2026, 5, 4, 11, tzinfo=UTC)
    b_e = datetime(2026, 5, 4, 12, tzinfo=UTC)
    assert _overlaps(a_s, a_e, b_s, b_e) is False


def test_coerce_dt_promotes_naive_datetime_to_utc():
    naive = datetime(2026, 5, 4, 9, 0)
    out = _coerce_dt(naive)
    assert out.tzinfo == UTC


def test_coerce_dt_passes_through_aware_datetime():
    aware = datetime(2026, 5, 4, 9, 0, tzinfo=UTC)
    assert _coerce_dt(aware) is aware


# ── CalendarFetcher constructor + from_settings ────────────────────────────


def test_fetcher_requires_url():
    with pytest.raises(CalendarFetchError):
        CalendarFetcher(url="")


class _FakeSecrets:
    def __init__(self, store: dict):
        self._store = store

    def get(self, ns: str, key: str):
        return self._store.get(ns, {}).get(key)


def test_from_settings_raises_when_disabled():
    s = AgentSettings()
    with pytest.raises(CalendarFetchError, match="enabled"):
        CalendarFetcher.from_settings(s, _FakeSecrets({}))


def test_from_settings_raises_when_url_missing():
    s = AgentSettings()
    s.calendar.enabled = True
    with pytest.raises(CalendarFetchError, match="ICS URL"):
        CalendarFetcher.from_settings(s, _FakeSecrets({}))


def test_from_settings_builds_when_complete():
    s = AgentSettings()
    s.calendar.enabled = True
    fetcher = CalendarFetcher.from_settings(
        s, _FakeSecrets({"calendar": {"ics_url": "https://example.com/cal.ics"}})
    )
    assert fetcher.url == "https://example.com/cal.ics"


# ── fetch_events with mocked httpx ─────────────────────────────────────────


def test_fetch_events_returns_empty_on_network_failure(monkeypatch):
    fetcher = CalendarFetcher(url="https://invalid.example/not-real.ics")

    def _boom(url, *, timeout):
        raise OSError("dns failure")

    monkeypatch.setattr("httpx.get", _boom)
    out = fetcher.fetch_events(
        start=datetime(2026, 5, 4, tzinfo=UTC),
        end=datetime(2026, 5, 5, tzinfo=UTC),
    )
    assert out == []


def test_fetch_events_parses_returned_ics(monkeypatch):
    raw = _ics(_vevent_single())

    class _Resp:
        content = raw

        def raise_for_status(self):
            return None

    monkeypatch.setattr("httpx.get", lambda url, *, timeout: _Resp())
    fetcher = CalendarFetcher(url="https://example.com/cal.ics")

    out = fetcher.fetch_events(
        start=datetime(2026, 5, 4, tzinfo=UTC),
        end=datetime(2026, 5, 5, tzinfo=UTC),
    )
    assert len(out) == 1
    assert out[0].summary == "Q2 review"


def test_fetch_events_returns_empty_on_parse_error(monkeypatch):
    class _Resp:
        content = b"not an ICS file"

        def raise_for_status(self):
            return None

    monkeypatch.setattr("httpx.get", lambda url, *, timeout: _Resp())
    fetcher = CalendarFetcher(url="https://example.com/cal.ics")

    out = fetcher.fetch_events(
        start=datetime(2026, 5, 4, tzinfo=UTC),
        end=datetime(2026, 5, 5, tzinfo=UTC),
    )
    assert out == []


# ── Convenience wrappers ───────────────────────────────────────────────────


def test_fetch_today_uses_utc_midnight_window(monkeypatch):
    raw = _ics(_vevent_single())  # event at 14:00 UTC on 2026-05-04

    class _Resp:
        content = raw

        def raise_for_status(self):
            return None

    monkeypatch.setattr("httpx.get", lambda url, *, timeout: _Resp())
    fetcher = CalendarFetcher(url="https://example.com/cal.ics")

    fixed_now = datetime(2026, 5, 4, 11, 0, tzinfo=UTC)
    out = fetch_today(fetcher, now=fixed_now)
    assert len(out) == 1
    assert out[0].summary == "Q2 review"


def test_fetch_window_arbitrary_hours(monkeypatch):
    raw = _ics(_vevent_recurring_daily(count=10))

    class _Resp:
        content = raw

        def raise_for_status(self):
            return None

    monkeypatch.setattr("httpx.get", lambda url, *, timeout: _Resp())
    fetcher = CalendarFetcher(url="https://example.com/cal.ics")

    fixed_now = datetime(2026, 5, 4, 9, 0, tzinfo=UTC)
    # 48-hour window starting 09:00 → covers May 4 10:00 + May 5 10:00
    out = fetch_window(fetcher, hours=48, now=fixed_now)
    assert len(out) == 2
