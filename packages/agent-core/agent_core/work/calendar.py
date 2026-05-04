"""ICS calendar fetcher — read-only schedule for grounding chat + digest.

Sprint 23: gives the agent calendar awareness. When the user asks "what's
on my plate today?" or the digest renders, today's events join obligations
in the context. Read-only — no write access — so this never accidentally
modifies your calendar.

Source: an ICS feed URL ("secret address in iCal format" in Google Cal
parlance). The same single URL works for Google, iCloud, Fastmail, any
standard CalDAV-style provider that serves ICS. The URL itself is the
secret; stash it in the secret store, never commit to settings.

Recurrence: ``icalendar`` parses RRULE; we expand occurrences ourselves
into a window using ``dateutil.rrule`` (already a transitive dep via
icalendar). Common cases (DAILY/WEEKLY/MONTHLY with UNTIL or COUNT)
work fully; exotic BYxxx combos may render as the first instance only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Any

import httpx

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    pass


# ── Errors ──────────────────────────────────────────────────────────────────


class CalendarFetchError(RuntimeError):
    """Configuration or network error during calendar fetch."""


# ── Data ────────────────────────────────────────────────────────────────────


@dataclass
class CalendarEvent:
    """One calendar event instance, normalized to UTC for storage.

    All-day events have ``start.tzinfo is None`` and span midnight to
    midnight in the calendar's local view; we keep them as naive
    UTC-equivalent datetimes with the all_day flag set.
    """

    uid: str
    summary: str
    start: datetime
    end: datetime
    all_day: bool = False
    location: str = ""
    description: str = ""
    organizer: str = ""
    attendees: list[str] = field(default_factory=list)


@dataclass
class FetchEventsReport:
    fetched: int = 0
    in_window: int = 0
    errors: list[str] = field(default_factory=list)


# ── Fetcher ─────────────────────────────────────────────────────────────────


class CalendarFetcher:
    """Fetch + parse + expand a single ICS feed for a date window."""

    def __init__(
        self,
        *,
        url: str,
        timeout_seconds: float = 15.0,
    ) -> None:
        if not url:
            raise CalendarFetchError("CalendarFetcher requires a non-empty url")
        self.url = url
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_settings(cls, settings: Any, secrets: Any) -> "CalendarFetcher":
        cal = settings.calendar
        if not cal.enabled:
            raise CalendarFetchError(
                "calendar.enabled is False — set it via "
                "`dcos settings set calendar.enabled=true`"
            )
        url = secrets.get("calendar", cal.ics_url_secret_key)
        if not url:
            raise CalendarFetchError(
                f"no ICS URL in secrets store under "
                f"namespace='calendar' key='{cal.ics_url_secret_key}'. "
                "Set it: `dcos secrets set calendar.ics_url=<https-url>`."
            )
        return cls(url=url, timeout_seconds=cal.timeout_seconds)

    # ── Fetch ───────────────────────────────────────────────────────────────

    def fetch_raw(self) -> bytes | None:
        """Download the ICS body. Returns None on network failure (logged)."""
        try:
            resp = httpx.get(self.url, timeout=self.timeout_seconds)
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            logger.warning("calendar fetch failed: %s", e)
            return None

    def fetch_events(
        self,
        *,
        start: datetime,
        end: datetime,
    ) -> list[CalendarEvent]:
        """Fetch the feed and return events within ``[start, end)``.

        Both ``start`` and ``end`` should be timezone-aware. Recurring
        events are expanded into discrete instances within the window.
        Never raises into callers — network/parse failures yield [].
        """
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)

        raw = self.fetch_raw()
        if raw is None:
            return []
        try:
            return _parse_and_expand(raw, start=start, end=end)
        except Exception as e:
            logger.warning("calendar parse failed: %s", e)
            return []


# ── Parser + RRULE expander ────────────────────────────────────────────────


def _parse_and_expand(
    raw: bytes, *, start: datetime, end: datetime
) -> list[CalendarEvent]:
    """Turn raw ICS bytes into a list of CalendarEvent instances within
    [start, end). Expands RRULEs."""
    from icalendar import Calendar

    cal = Calendar.from_ical(raw)
    out: list[CalendarEvent] = []

    for component in cal.walk("VEVENT"):
        try:
            occurrences = _expand_event(component, window_start=start, window_end=end)
        except Exception as e:
            logger.debug("skipping VEVENT: %s", e)
            continue
        out.extend(occurrences)

    out.sort(key=lambda e: e.start)
    return out


def _expand_event(
    component: Any, *, window_start: datetime, window_end: datetime
) -> list[CalendarEvent]:
    """Expand one VEVENT into zero or more concrete CalendarEvent rows.

    Single events: include if they overlap the window.
    Recurring (RRULE): use dateutil.rrule.rrulestr to expand within the window.
    """
    from dateutil.rrule import rrulestr

    dtstart = component.get("DTSTART")
    if dtstart is None:
        return []
    dtend = component.get("DTEND")

    base_start = _coerce_dt(dtstart.dt)
    if dtend is not None:
        base_end = _coerce_dt(dtend.dt)
    else:
        # No DTEND — assume same as DTSTART (zero-length) for sanity
        base_end = base_start
    duration = base_end - base_start
    all_day = isinstance(dtstart.dt, date) and not isinstance(dtstart.dt, datetime)

    summary = str(component.get("SUMMARY") or "(no title)")
    location = str(component.get("LOCATION") or "")
    description = str(component.get("DESCRIPTION") or "")
    uid = str(component.get("UID") or "")
    organizer = _strip_mailto(str(component.get("ORGANIZER") or ""))
    attendees_raw = component.get("ATTENDEE", [])
    if not isinstance(attendees_raw, list):
        attendees_raw = [attendees_raw]
    attendees = [_strip_mailto(str(a)) for a in attendees_raw]

    rrule = component.get("RRULE")
    out: list[CalendarEvent] = []

    if rrule is None:
        # Single-instance event
        if _overlaps(base_start, base_end, window_start, window_end):
            out.append(
                _build_event(
                    uid=uid,
                    summary=summary,
                    start=base_start,
                    end=base_end,
                    all_day=all_day,
                    location=location,
                    description=description,
                    organizer=organizer,
                    attendees=attendees,
                )
            )
        return out

    # Recurring — let dateutil do the heavy lifting.
    rule_str = rrule.to_ical().decode() if hasattr(rrule, "to_ical") else str(rrule)
    try:
        rule = rrulestr(rule_str, dtstart=base_start)
    except Exception as e:
        logger.debug("rrule parse failed for %r: %s", summary, e)
        if _overlaps(base_start, base_end, window_start, window_end):
            out.append(
                _build_event(
                    uid=uid,
                    summary=summary,
                    start=base_start,
                    end=base_end,
                    all_day=all_day,
                    location=location,
                    description=description,
                    organizer=organizer,
                    attendees=attendees,
                )
            )
        return out

    # Expand within an extended window so events that *start* before but
    # *end* within the window aren't missed; we filter by overlap below.
    expand_from = window_start - duration
    occurrences = rule.between(expand_from, window_end, inc=True)
    for occ_start in occurrences:
        occ_start = _coerce_dt(occ_start)
        occ_end = occ_start + duration
        if not _overlaps(occ_start, occ_end, window_start, window_end):
            continue
        out.append(
            _build_event(
                uid=f"{uid}@{occ_start.isoformat()}" if uid else uid,
                summary=summary,
                start=occ_start,
                end=occ_end,
                all_day=all_day,
                location=location,
                description=description,
                organizer=organizer,
                attendees=attendees,
            )
        )
    return out


def _build_event(**kwargs) -> CalendarEvent:
    return CalendarEvent(**kwargs)


def _coerce_dt(value: Any) -> datetime:
    """Ensure we have a tz-aware datetime; promote dates to midnight UTC."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, date):
        return datetime.combine(value, time(0, 0), tzinfo=timezone.utc)
    raise TypeError(f"can't coerce {type(value).__name__} to datetime")


def _overlaps(
    a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime
) -> bool:
    return a_start < b_end and a_end > b_start


def _strip_mailto(value: str) -> str:
    if value.lower().startswith("mailto:"):
        return value[len("mailto:") :]
    return value


# ── Convenience: today's schedule ──────────────────────────────────────────


def fetch_today(
    fetcher: CalendarFetcher, *, now: datetime | None = None
) -> list[CalendarEvent]:
    """Return events whose start/end overlaps today (UTC midnight to
    next-midnight)."""
    if now is None:
        now = datetime.now(timezone.utc)
    start = datetime.combine(now.date(), time(0, 0), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return fetcher.fetch_events(start=start, end=end)


def fetch_window(
    fetcher: CalendarFetcher, *, hours: int, now: datetime | None = None
) -> list[CalendarEvent]:
    """Return events overlapping ``[now, now + hours)``."""
    if now is None:
        now = datetime.now(timezone.utc)
    return fetcher.fetch_events(start=now, end=now + timedelta(hours=hours))


__all__ = [
    "CalendarEvent",
    "CalendarFetchError",
    "CalendarFetcher",
    "FetchEventsReport",
    "fetch_today",
    "fetch_window",
]
