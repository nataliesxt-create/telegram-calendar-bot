"""
Clash detection and free-slot suggestion.

Checks a proposed event time against all calendars and, if there is a
clash, suggests three alternative free slots of the same duration.
"""

from __future__ import annotations

import datetime

from calendar_service import get_events_in_range
from config import TZ


def check_clash(
    service,
    calendars: list[dict],
    start_dt: datetime.datetime,
    duration_minutes: int,
    exclude_event_id: str | None = None,
) -> dict | None:
    """
    Check whether a proposed time slot clashes with any existing event.

    Args:
        service:          Google Calendar API service client.
        calendars:        Full list of user calendars.
        start_dt:         Proposed event start (timezone-aware).
        duration_minutes: Proposed event duration.
        exclude_event_id: Skip this event ID when checking (used when editing).

    Returns:
        The first clashing event dict, or None if the slot is free.
    """
    end_dt = start_dt + datetime.timedelta(minutes=duration_minutes)
    existing = get_events_in_range(service, calendars, start_dt, end_dt)

    for ev in existing:
        if exclude_event_id and ev["id"] == exclude_event_id:
            continue
        # Overlap condition: existing.start < proposed.end AND existing.end > proposed.start
        if ev["start"] < end_dt and ev["end"] > start_dt:
            return ev

    return None


def find_free_slots(
    service,
    calendars: list[dict],
    anchor_dt: datetime.datetime,
    duration_minutes: int,
    n: int = 3,
    exclude_event_id: str | None = None,
) -> list[datetime.datetime]:
    """
    Find the next `n` free time slots of `duration_minutes` starting from `anchor_dt`.

    Searches forward in 30-minute increments with no working-hours restriction.

    Args:
        service:          Google Calendar API service.
        calendars:        All user calendars.
        anchor_dt:        Where to start searching (usually the clashing slot).
        duration_minutes: Duration the free slot must accommodate.
        n:                How many free slots to return.
        exclude_event_id: Ignore this event ID (for edits).

    Returns:
        List of free slot start datetimes (timezone-aware, in user's TZ).
    """
    STEP = datetime.timedelta(minutes=30)
    MAX_DAYS = 14  # give up after 2 weeks

    slots: list[datetime.datetime] = []
    candidate = anchor_dt + datetime.timedelta(minutes=duration_minutes)

    # Round candidate up to next 30-min boundary
    candidate = _round_up_to_slot(candidate)

    deadline = anchor_dt + datetime.timedelta(days=MAX_DAYS)

    while len(slots) < n and candidate < deadline:
        end_candidate = candidate + datetime.timedelta(minutes=duration_minutes)
        existing = get_events_in_range(service, calendars, candidate, end_candidate)

        clash = False
        for ev in existing:
            if exclude_event_id and ev["id"] == exclude_event_id:
                continue
            if ev["start"] < end_candidate and ev["end"] > candidate:
                clash = True
                # Jump past this event
                candidate = _round_up_to_slot(ev["end"])
                break

        if not clash:
            slots.append(candidate)
            candidate += STEP

    return slots


def format_clash_message(
    clashing_event: dict,
    free_slots: list[datetime.datetime],
) -> str:
    """
    Build the clash warning message with numbered alternative slots.

    Args:
        clashing_event: The event that occupies the requested time.
        free_slots:     List of suggested alternative datetimes.

    Returns:
        Formatted string ready to send via Telegram.
    """
    clash_start = clashing_event["start"].strftime("%-I:%M %p")
    clash_end = clashing_event["end"].strftime("%-I:%M %p")
    title = clashing_event.get("title", "another event")

    lines = [
        f"⚠️ Clash with *{title}* ({clash_start}–{clash_end}).",
        "Here are 3 next free slots:",
    ]
    for i, slot in enumerate(free_slots, 1):
        day_str = slot.strftime("%a %d %b")
        time_str = slot.strftime("%-I:%M %p")
        lines.append(f"{i}. {day_str}, {time_str}")

    lines.append("\nReply with *1*, *2*, or *3* to book, or *'keep original'* to override.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _round_up_to_slot(dt: datetime.datetime, step_minutes: int = 30) -> datetime.datetime:
    """Round a datetime up to the next N-minute boundary."""
    step = datetime.timedelta(minutes=step_minutes)
    remainder = datetime.timedelta(
        minutes=dt.minute % step_minutes,
        seconds=dt.second,
        microseconds=dt.microsecond,
    )
    if remainder == datetime.timedelta(0):
        return dt
    return dt - remainder + step
