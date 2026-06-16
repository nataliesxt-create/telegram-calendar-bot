"""
Google Calendar API wrapper.

All methods accept/return plain Python dicts so the rest of the app
never imports google API types directly.
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from config import CLASH_EXCLUDED_CALENDARS, DEFAULT_CALENDAR, LOOKAHEAD_DAYS, TIMEZONE, TZ

# Google Calendar color IDs
COLOR_GREEN = "10"  # Basil → confirmed appointment (standard green in Google Calendar)
COLOR_RED   = "11"  # Tomato → placeholder block

APPOINTMENT_PLACEHOLDER_PREFIX = "Appointment:"
APPOINTMENTS_CALENDAR_NAME = "Appointments"
APPOINTMENT_DURATION_MINUTES = 90


# ---------------------------------------------------------------------------
# Service factory
# ---------------------------------------------------------------------------

def build_service(creds: Credentials):
    """Return a Google Calendar API service client."""
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# Calendar list
# ---------------------------------------------------------------------------

def list_calendars(service) -> list[dict]:
    """
    Return all calendars in the user's account.

    Each dict: {id, name, primary (bool)}.
    """
    result = service.calendarList().list().execute()
    items = result.get("items", [])
    return [
        {
            "id": cal["id"],
            "name": cal.get("summary", cal["id"]),
            "primary": cal.get("primary", False),
        }
        for cal in items
    ]


def find_calendar_id(calendars: list[dict], hint: str | None) -> str:
    """
    Resolve a calendar name hint to a calendar ID.

    Falls back to primary calendar if hint is None or no match found.
    """
    if not hint:
        return DEFAULT_CALENDAR

    hint_lower = hint.lower()
    for cal in calendars:
        if hint_lower in cal["name"].lower():
            return cal["id"]

    # Check for primary
    for cal in calendars:
        if cal.get("primary"):
            return cal["id"]

    return DEFAULT_CALENDAR


# ---------------------------------------------------------------------------
# Event CRUD
# ---------------------------------------------------------------------------

def create_event(
    service,
    calendar_id: str,
    title: str,
    start_dt: datetime.datetime,
    duration_minutes: int,
    color_id: str | None = None,
) -> dict:
    """
    Create a calendar event and return the created event dict.

    Returns: {id, title, start, end, calendar_id, html_link}
    """
    end_dt = start_dt + datetime.timedelta(minutes=duration_minutes)
    body = {
        "summary": title,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": TIMEZONE},
    }
    if color_id:
        body["colorId"] = color_id
    event = service.events().insert(calendarId=calendar_id, body=body).execute()
    return _normalise(event, calendar_id)


def update_event(
    service,
    calendar_id: str,
    event_id: str,
    *,
    title: str | None = None,
    start_dt: datetime.datetime | None = None,
    duration_minutes: int | None = None,
    color_id: str | None = None,
) -> dict:
    """
    Patch an existing event.  Only the supplied kwargs are changed.

    Returns: normalised event dict.
    """
    existing = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
    patch: dict = {}

    if title:
        patch["summary"] = title

    if color_id:
        patch["colorId"] = color_id

    if start_dt:
        if duration_minutes is None:
            # Preserve original duration
            orig_start = _parse_google_dt(existing["start"])
            orig_end = _parse_google_dt(existing["end"])
            duration_minutes = int((orig_end - orig_start).total_seconds() / 60)
        end_dt = start_dt + datetime.timedelta(minutes=duration_minutes)
        patch["start"] = {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE}
        patch["end"] = {"dateTime": end_dt.isoformat(), "timeZone": TIMEZONE}

    updated = (
        service.events()
        .patch(calendarId=calendar_id, eventId=event_id, body=patch)
        .execute()
    )
    return _normalise(updated, calendar_id)


def delete_event(service, calendar_id: str, event_id: str) -> None:
    """Delete a calendar event by ID."""
    service.events().delete(calendarId=calendar_id, eventId=event_id).execute()


# ---------------------------------------------------------------------------
# Query / search
# ---------------------------------------------------------------------------

def get_events_on_day(
    service,
    calendars: list[dict],
    date: datetime.date,
) -> list[dict]:
    """
    Return all events on a given date across all calendars, sorted by start time.

    Each dict includes a 'calendar_name' key.
    """
    day_start = datetime.datetime(date.year, date.month, date.day, 0, 0, 0, tzinfo=TZ)
    day_end = day_start + datetime.timedelta(days=1)

    events: list[dict] = []
    for cal in calendars:
        result = (
            service.events()
            .list(
                calendarId=cal["id"],
                timeMin=day_start.isoformat(),
                timeMax=day_end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        for ev in result.get("items", []):
            norm = _normalise(ev, cal["id"])
            norm["calendar_name"] = cal["name"]
            events.append(norm)

    events.sort(key=lambda e: e["start"])
    return events


def search_events(
    service,
    calendars: list[dict],
    query: str,
    date: datetime.date | None = None,
) -> list[dict]:
    """
    Search for events matching `query` across all calendars.

    Optionally restrict to a ±3-day window around `date`.
    Returns normalised event dicts including 'calendar_name'.
    """
    today = datetime.date.today()
    if date:
        window_start = datetime.datetime(
            date.year, date.month, date.day, tzinfo=TZ
        ) - datetime.timedelta(days=3)
        window_end = window_start + datetime.timedelta(days=7)
    else:
        window_start = datetime.datetime.now(tz=TZ) - datetime.timedelta(days=30)
        window_end = datetime.datetime.now(tz=TZ) + datetime.timedelta(
            days=LOOKAHEAD_DAYS
        )

    events: list[dict] = []
    for cal in calendars:
        result = (
            service.events()
            .list(
                calendarId=cal["id"],
                q=query,
                timeMin=window_start.isoformat(),
                timeMax=window_end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=10,
            )
            .execute()
        )
        for ev in result.get("items", []):
            norm = _normalise(ev, cal["id"])
            norm["calendar_name"] = cal["name"]
            events.append(norm)

    events.sort(key=lambda e: e["start"])
    return events


def get_events_in_range(
    service,
    calendars: list[dict],
    start_dt: datetime.datetime,
    end_dt: datetime.datetime,
) -> list[dict]:
    """
    Return all events across all calendars within [start_dt, end_dt].

    Excludes calendars listed in CLASH_EXCLUDED_CALENDARS. Used by clash_detector.
    """
    events: list[dict] = []
    for cal in calendars:
        if cal["name"].lower() in CLASH_EXCLUDED_CALENDARS or cal["id"].lower() in CLASH_EXCLUDED_CALENDARS:
            continue
        result = (
            service.events()
            .list(
                calendarId=cal["id"],
                timeMin=start_dt.isoformat(),
                timeMax=end_dt.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        for ev in result.get("items", []):
            norm = _normalise(ev, cal["id"])
            norm["calendar_name"] = cal["name"]
            events.append(norm)

    events.sort(key=lambda e: e["start"])
    return events


# ---------------------------------------------------------------------------
# Appointments calendar helpers
# ---------------------------------------------------------------------------

def find_appointments_calendar(calendars: list[dict]) -> dict | None:
    """Return the Appointments calendar dict, or None if not found."""
    for cal in calendars:
        if APPOINTMENTS_CALENDAR_NAME.lower() in cal["name"].lower():
            return cal
    return None


def find_placeholder_slots(
    service,
    appointments_cal: dict,
    on_date: datetime.date | None = None,
    limit: int = 5,
) -> list[dict]:
    """
    Find upcoming red placeholder blocks in the Appointments calendar.

    These are events whose title starts with 'Appointment:' and have
    colorId == COLOR_RED (11 / Tomato), or no specific color set (inherits
    calendar color which may be red).

    Args:
        service:          Google Calendar API service.
        appointments_cal: The Appointments calendar dict.
        on_date:          If provided, only look on this date.
        limit:            Max number of placeholders to return.

    Returns:
        List of normalised event dicts, sorted by start time.
    """
    now = datetime.datetime.now(tz=TZ)

    if on_date:
        time_min = datetime.datetime(on_date.year, on_date.month, on_date.day, tzinfo=TZ)
        time_max = time_min + datetime.timedelta(days=1)
    else:
        time_min = now
        time_max = now + datetime.timedelta(days=LOOKAHEAD_DAYS)

    result = (
        service.events()
        .list(
            calendarId=appointments_cal["id"],
            q=APPOINTMENT_PLACEHOLDER_PREFIX,
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=limit * 3,  # fetch extra to filter
        )
        .execute()
    )

    placeholders = []
    for ev in result.get("items", []):
        title = ev.get("summary", "")
        # Must start with "Appointment:" prefix
        if not title.strip().startswith(APPOINTMENT_PLACEHOLDER_PREFIX):
            continue
        # Must still be a placeholder (red color or no override color = calendar default)
        color = ev.get("colorId", COLOR_RED)
        if color == COLOR_GREEN:
            continue  # already booked
        norm = _normalise(ev, appointments_cal["id"])
        norm["calendar_name"] = appointments_cal["name"]
        placeholders.append(norm)
        if len(placeholders) >= limit:
            break

    return placeholders


def book_appointment_slot(
    service,
    appointments_cal: dict,
    placeholder_event_id: str,
    client_name: str,
    start_dt: datetime.datetime,
    duration_minutes: int = APPOINTMENT_DURATION_MINUTES,
) -> dict:
    """
    Replace a red placeholder block with a confirmed appointment.

    Renames the event to 'Appointment: [client_name]', sets duration to
    duration_minutes, and changes color to green.

    Returns: updated normalised event dict.
    """
    end_dt = start_dt + datetime.timedelta(minutes=duration_minutes)
    patch = {
        "summary": f"Appointment: {client_name}",
        "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": TIMEZONE},
        "colorId": COLOR_GREEN,
    }
    updated = (
        service.events()
        .patch(
            calendarId=appointments_cal["id"],
            eventId=placeholder_event_id,
            body=patch,
        )
        .execute()
    )
    norm = _normalise(updated, appointments_cal["id"])
    norm["calendar_name"] = appointments_cal["name"]
    return norm


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalise(event: dict, calendar_id: str) -> dict:
    """Convert a raw Google Calendar event dict to a simpler internal format."""
    start = _parse_google_dt(event.get("start", {}))
    end = _parse_google_dt(event.get("end", {}))
    return {
        "id": event.get("id", ""),
        "title": event.get("summary", "(No title)"),
        "start": start,
        "end": end,
        "calendar_id": calendar_id,
        "html_link": event.get("htmlLink", ""),
    }


def _parse_google_dt(dt_field: dict) -> datetime.datetime:
    """Parse a Google Calendar start/end field (dateTime or date) to a datetime."""
    if "dateTime" in dt_field:
        dt = datetime.datetime.fromisoformat(dt_field["dateTime"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    if "date" in dt_field:
        d = datetime.date.fromisoformat(dt_field["date"])
        return datetime.datetime(d.year, d.month, d.day, tzinfo=TZ)
    return datetime.datetime.now(tz=TZ)
