"""
Telegram Calendar Agent Bot — main entry point.

Wires together auth, intent parsing, calendar operations, clash detection,
and session memory into a fully async python-telegram-bot v20+ application.
"""

from __future__ import annotations

import datetime
import json
import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import io
import tempfile

import auth
import calendar_service
import clash_detector
import intent_parser
import session_memory as sm_module
from config import (
    CHAT_ID_PATH,
    DEFAULT_DURATION_MINUTES,
    NOTIFIED_FOLLOWUPS_PATH,
    OPENAI_API_KEY,
    TELEGRAM_TOKEN,
    TIMEZONE,
    TZ,
)
from intent_parser import UNKNOWN_REPLY, _client as openai_client

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global shared state (single-user bot)
# ---------------------------------------------------------------------------

session_memory = sm_module.SessionMemory()
_calendars_cache: list[dict] = []  # populated on first authenticated request


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_service_and_calendars():
    """
    Load Google credentials, build the API service, and refresh the calendars cache.

    Returns:
        (service, calendars) or (None, None) if not authenticated.
    """
    global _calendars_cache
    creds = auth.load_credentials()
    if not creds:
        return None, None
    service = calendar_service.build_service(creds)
    if not _calendars_cache:
        _calendars_cache = calendar_service.list_calendars(service)
    return service, _calendars_cache


def _resolve_datetime(intent: dict) -> datetime.datetime | None:
    """
    Convert parsed intent date + time fields to a timezone-aware datetime.

    Applies ambiguous time-of-day defaults from config.
    """
    from config import TIME_DEFAULTS

    date_str: str | None = intent.get("date")
    time_str: str | None = intent.get("time")

    if not date_str:
        return None

    try:
        date = datetime.date.fromisoformat(date_str)
    except ValueError:
        return None

    if not time_str:
        # Try to infer from original message
        msg = (intent.get("original_message") or "").lower()
        for keyword, default_time in TIME_DEFAULTS.items():
            if keyword in msg:
                time_str = default_time
                break
        if not time_str:
            time_str = "09:00"  # absolute fallback

    try:
        t = datetime.time.fromisoformat(time_str)
    except ValueError:
        t = datetime.time(9, 0)

    return datetime.datetime(date.year, date.month, date.day, t.hour, t.minute, tzinfo=TZ)


def _format_event_line(event: dict, include_calendar: bool = True) -> str:
    """Format a single event for display in a schedule list."""
    time_str = event["start"].strftime("%-I:%M %p")
    line = f"{time_str} – {event['title']}"
    if include_calendar and event.get("calendar_name"):
        line += f" · {event['calendar_name']}"
    return line


def _format_date_header(dt: datetime.datetime) -> str:
    """Return a friendly date string like 'Monday 9 Jun 2026'."""
    return dt.strftime("%A %-d %b %Y")


async def _require_auth(update: Update, user_id: int) -> bool:
    """
    Check authentication status. If not authenticated, prompt the user and return False.
    """
    creds = auth.load_credentials()
    if creds:
        return True
    await update.effective_message.reply_text(
        "You need to connect Google Calendar first.\n"
        "Use /start to get your authorisation link."
    )
    return False


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /start — send a Google OAuth link or confirm already authenticated.
    """
    user_id = update.effective_user.id
    creds = auth.load_credentials()

    if creds:
        await update.effective_message.reply_text(
            "✅ Hey! I'm *Ouni* and I'm already connected to your Google Calendar.\n"
            "Just tell me what you need — text or voice note works!",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    auth_url, flow = auth.get_auth_url()
    session = session_memory.get(user_id)
    session.pending_auth_flow = flow

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔗 Authorise Google Calendar", url=auth_url)
    ]])
    await update.effective_message.reply_text(
        "👋 Hi, I'm *Ouni* — your personal calendar assistant.\n\n"
        "Tap the button below to connect your Google Calendar.\n"
        "After authorising, copy the code Google gives you and send it here.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /reset — clear session state (pending clashes, picks, bookings).
    """
    user_id = update.effective_user.id
    session_memory.clear(user_id)
    await update.effective_message.reply_text("🔄 Session reset. What would you like to do?")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /help — display usage examples.
    """
    await update.effective_message.reply_text(
        "*Ouni* — your calendar assistant 📅\n\n"
        "📅 *Add:* 'Lash appointment with Jermsy Beauty on 22nd June at 8am'\n"
        "📋 *Book client:* 'Appointment with Nicholas at 1pm Thursday'\n"
        "✏️ *Edit:* 'Move my 3pm call to 5pm'\n"
        "🗑️ *Delete:* 'Cancel my dentist on Friday'\n"
        "👀 *View:* 'What do I have tomorrow?' or 'Show me Thursday grouped by calendar'\n"
        "🔁 *Correct:* 'Actually make it 4pm' or 'Add it to Work instead'\n"
        "🎙️ *Voice notes supported* — just hold and speak",
        parse_mode=ParseMode.MARKDOWN,
    )


# ---------------------------------------------------------------------------
# Appointments calendar booking flow
# ---------------------------------------------------------------------------

async def _handle_book_appointment(
    update: Update,
    user_id: int,
    intent: dict,
    service,
    calendars: list[dict],
) -> None:
    """
    Book a client/personal appointment into the Appointments calendar.

    Flow:
    - If date+time given: find placeholder at that slot → confirm with user
    - If no time given: find next available red placeholder → propose it
    - On user confirmation (yes/y): replace placeholder with confirmed event (green)
    """
    appt_cal = calendar_service.find_appointments_calendar(calendars)
    if not appt_cal:
        await update.effective_message.reply_text(
            "❌ I couldn't find an 'Appointments' calendar. Please create one in Google Calendar first."
        )
        return

    client_name = intent.get("title") or "Appointment"
    date_str = intent.get("date")
    time_str = intent.get("time")

    await update.effective_message.reply_text("🔍 Checking your Appointments calendar...")

    target_dt = _resolve_datetime(intent) if (date_str and time_str) else None
    on_date = target_dt.date() if target_dt else (datetime.date.fromisoformat(date_str) if date_str else None)

    # Find overlapping placeholder on that date (to delete it after booking)
    placeholders = calendar_service.find_placeholder_slots(service, appt_cal, on_date=on_date)

    if target_dt:
        # User specified a time — book at their time, find any overlapping placeholder to clean up
        end_dt = target_dt + datetime.timedelta(minutes=calendar_service.APPOINTMENT_DURATION_MINUTES)
        overlapping_placeholder = None
        for p in placeholders:
            if p["start"] < end_dt and p["end"] > target_dt:
                overlapping_placeholder = p
                break

        await _propose_appointment(
            update, user_id, client_name, appt_cal,
            book_at=target_dt,
            placeholder_to_delete=overlapping_placeholder,
        )
    else:
        # No time — propose the next available placeholder's time
        if not placeholders:
            msg = "No available appointment slots found"
            msg += f" on {on_date.strftime('%a %-d %b')}" if on_date else " coming up"
            await update.effective_message.reply_text(f"❌ {msg}. Add a red 'Appointment:' block to your calendar first.")
            return

        p = placeholders[0]
        await _propose_appointment(
            update, user_id, client_name, appt_cal,
            book_at=p["start"],
            placeholder_to_delete=p,
        )


async def _propose_appointment(
    update: Update,
    user_id: int,
    client_name: str,
    appt_cal: dict,
    book_at: datetime.datetime,
    placeholder_to_delete: dict | None,
) -> None:
    """Send a confirmation prompt before booking an appointment."""
    end_dt = book_at + datetime.timedelta(minutes=calendar_service.APPOINTMENT_DURATION_MINUTES)
    day_str = _format_date_header(book_at)
    time_str = f"{book_at.strftime('%-I:%M %p')}–{end_dt.strftime('%-I:%M %p')}"

    s = session_memory.get(user_id)
    s.pending_appointment_booking = {
        "placeholder_id": placeholder_to_delete["id"] if placeholder_to_delete else None,
        "client_name": client_name,
        "start_dt": book_at,
        "appt_cal_id": appt_cal["id"],
        "appt_cal_name": appt_cal["name"],
    }

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, book it", callback_data="appt:yes"),
            InlineKeyboardButton("❌ Cancel", callback_data="appt:no"),
        ]
    ])
    await update.effective_message.reply_text(
        f"📅 *{day_str}* at {time_str}\n\n"
        f"Book *Appointment: {client_name}* here?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# Core action handlers
# ---------------------------------------------------------------------------

async def _handle_create(
    update: Update,
    user_id: int,
    intent: dict,
    service,
    calendars: list[dict],
    start_dt: datetime.datetime,
    duration_minutes: int,
    calendar_id: str,
    calendar_name: str,
) -> None:
    """Create a new calendar event, handling clash detection."""
    clashing = clash_detector.check_clash(service, calendars, start_dt, duration_minutes)

    if clashing:
        free_slots = clash_detector.find_free_slots(
            service, calendars, start_dt, duration_minutes
        )
        s = session_memory.get(user_id)
        s.pending_clash = True
        s.clash_slots = free_slots
        s.clash_pending_action = "create"
        s.pending_intent = intent
        # Store resolved values for when user picks a slot
        s.title = intent.get("title")
        s.duration_minutes = duration_minutes
        s.calendar_id = calendar_id
        s.calendar_name = calendar_name
        s.start_dt = start_dt

        await _send_clash_buttons(update, clashing, free_slots)
        return

    await update.effective_message.reply_text("🔍 Checking your calendar...")

    event = calendar_service.create_event(
        service, calendar_id, intent["title"], start_dt, duration_minutes
    )

    day_str = _format_date_header(start_dt)
    time_str = start_dt.strftime("%-I:%M %p")
    await update.effective_message.reply_text(
        f"✅ Event created — *{event['title']}* on {day_str} at {time_str} · {calendar_name}",
        parse_mode=ParseMode.MARKDOWN,
    )
    session_memory.record_action(
        user_id,
        action="create",
        title=event["title"],
        start_dt=start_dt,
        duration_minutes=duration_minutes,
        calendar_id=calendar_id,
        calendar_name=calendar_name,
        event_id=event["id"],
    )


async def _handle_edit(
    update: Update,
    user_id: int,
    intent: dict,
    service,
    calendars: list[dict],
) -> None:
    """Edit an existing event by title, with multi-match selection if needed."""
    await update.effective_message.reply_text("🔍 Checking your calendar...")

    query = intent.get("title") or ""
    date_str = intent.get("date")
    search_date = datetime.date.fromisoformat(date_str) if date_str else None

    matches = calendar_service.search_events(service, calendars, query, search_date)

    if not matches:
        await update.effective_message.reply_text(f"❌ No event found matching '{query}'.")
        return

    if len(matches) > 1:
        s = session_memory.get(user_id)
        s.pending_matches = matches
        s.pending_match_action = "edit"
        s.pending_intent = intent
        await _send_match_buttons(update, matches, "Which event to edit?")
        return

    event = matches[0]
    await _apply_edit(update, user_id, intent, service, calendars, event)


async def _apply_edit(
    update: Update,
    user_id: int,
    intent: dict,
    service,
    calendars: list[dict],
    event: dict,
) -> None:
    """Apply edit to a specific resolved event."""
    new_start = _resolve_datetime(intent)
    new_duration = intent.get("duration_minutes")

    if new_start:
        clashing = clash_detector.check_clash(
            service, calendars, new_start, new_duration or DEFAULT_DURATION_MINUTES,
            exclude_event_id=event["id"]
        )
        if clashing:
            free_slots = clash_detector.find_free_slots(
                service, calendars, new_start,
                new_duration or DEFAULT_DURATION_MINUTES,
                exclude_event_id=event["id"],
            )
            s = session_memory.get(user_id)
            s.pending_clash = True
            s.clash_slots = free_slots
            s.clash_pending_action = "edit"
            s.pending_intent = intent
            s.event_id = event["id"]
            s.calendar_id = event["calendar_id"]
            s.calendar_name = event.get("calendar_name", "")
            s.title = event["title"]
            s.duration_minutes = new_duration or DEFAULT_DURATION_MINUTES
            s.start_dt = new_start

            await _send_clash_buttons(update, clashing, free_slots)
            return

    # Determine new calendar if hint provided
    new_calendar_id = event["calendar_id"]
    new_calendar_name = event.get("calendar_name", "")
    hint = intent.get("calendar_hint")
    if hint:
        new_calendar_id = calendar_service.find_calendar_id(calendars, hint)
        for cal in calendars:
            if cal["id"] == new_calendar_id:
                new_calendar_name = cal["name"]
                break

    updated = calendar_service.update_event(
        service,
        new_calendar_id,
        event["id"],
        title=intent.get("title") or event["title"],
        start_dt=new_start,
        duration_minutes=new_duration,
    )

    final_start = updated["start"]
    day_str = _format_date_header(final_start)
    time_str = final_start.strftime("%-I:%M %p")
    await update.effective_message.reply_text(
        f"📝 Updated — *{updated['title']}* now on {day_str} at {time_str} · {new_calendar_name}",
        parse_mode=ParseMode.MARKDOWN,
    )
    session_memory.record_action(
        user_id,
        action="edit",
        title=updated["title"],
        start_dt=final_start,
        duration_minutes=new_duration or DEFAULT_DURATION_MINUTES,
        calendar_id=new_calendar_id,
        calendar_name=new_calendar_name,
        event_id=updated["id"],
    )


async def _handle_delete(
    update: Update,
    user_id: int,
    intent: dict,
    service,
    calendars: list[dict],
) -> None:
    """Delete an event by title, with multi-match selection if needed."""
    await update.effective_message.reply_text("🔍 Checking your calendar...")

    query = intent.get("title") or ""
    date_str = intent.get("date")
    search_date = datetime.date.fromisoformat(date_str) if date_str else None

    matches = calendar_service.search_events(service, calendars, query, search_date)

    if not matches:
        await update.effective_message.reply_text("❌ No event found matching that.")
        return

    if len(matches) > 1:
        s = session_memory.get(user_id)
        s.pending_matches = matches
        s.pending_match_action = "delete"
        s.pending_intent = intent
        await _send_match_buttons(update, matches, "Which event to delete?")
        return

    event = matches[0]
    calendar_service.delete_event(service, event["calendar_id"], event["id"])
    day_str = event["start"].strftime("%a %-d %b")
    await update.effective_message.reply_text(
        f"🗑️ Deleted — *{event['title']}* on {day_str}",
        parse_mode=ParseMode.MARKDOWN,
    )
    session_memory.clear(user_id)


async def _handle_view(
    update: Update,
    user_id: int,
    intent: dict,
    service,
    calendars: list[dict],
) -> None:
    """Display all events on the requested date."""
    date_str = intent.get("date")
    if not date_str:
        date_str = datetime.date.today().isoformat()

    target_date = datetime.date.fromisoformat(date_str)
    events = calendar_service.get_events_on_day(service, calendars, target_date)

    day_label = datetime.datetime(
        target_date.year, target_date.month, target_date.day, tzinfo=TZ
    )
    header = _format_date_header(day_label)

    if not events:
        await update.effective_message.reply_text(f"📅 Nothing scheduled on {header}.")
        return

    msg_lower = (intent.get("original_message") or "").lower()
    group_by_calendar = "group" in msg_lower and "calendar" in msg_lower

    lines = [f"📅 *{header}*\n"]

    if group_by_calendar:
        grouped: dict[str, list[dict]] = {}
        for ev in events:
            cal_name = ev.get("calendar_name", "Other")
            grouped.setdefault(cal_name, []).append(ev)
        for cal_name, cal_events in grouped.items():
            lines.append(f"*{cal_name}*")
            for ev in cal_events:
                lines.append(_format_event_line(ev, include_calendar=False))
            lines.append("")
    else:
        for ev in events:
            lines.append(_format_event_line(ev))

    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def _handle_correct(
    update: Update,
    user_id: int,
    intent: dict,
    service,
    calendars: list[dict],
) -> None:
    """
    Apply a correction to the last action.

    Merges the correction intent over the last session state and re-executes.
    """
    s = session_memory.get(user_id)
    if not s.action or not s.event_id:
        await update.effective_message.reply_text(
            "I don't have a recent action to correct. Please describe what you'd like to change."
        )
        return

    # Build merged intent from session + correction
    merged = {
        "action": "edit",
        "title": intent.get("title") or s.title,
        "date": intent.get("date") or (s.date.isoformat() if s.date else None),
        "time": intent.get("time"),
        "duration_minutes": intent.get("duration_minutes") or s.duration_minutes,
        "calendar_hint": intent.get("calendar_hint"),
        "is_correction": True,
        "original_message": intent.get("original_message", ""),
    }

    event_stub = {
        "id": s.event_id,
        "title": s.title,
        "start": s.start_dt,
        "end": (
            (s.start_dt + datetime.timedelta(minutes=s.duration_minutes))
            if s.start_dt and s.duration_minutes
            else None
        ),
        "calendar_id": s.calendar_id,
        "calendar_name": s.calendar_name,
    }
    await _apply_edit(update, user_id, merged, service, calendars, event_stub)


# ---------------------------------------------------------------------------
# Inline keyboard helpers
# ---------------------------------------------------------------------------

async def _send_clash_buttons(
    update: Update,
    clashing: dict,
    free_slots: list[datetime.datetime],
) -> None:
    """Send clash warning with inline slot buttons."""
    clash_start = clashing["start"].strftime("%-I:%M %p")
    clash_end = clashing["end"].strftime("%-I:%M %p")
    lines = [f"⚠️ Clash with *{clashing['title']}* ({clash_start}–{clash_end})\n"]

    buttons = []
    for i, slot in enumerate(free_slots[:3]):
        label = slot.strftime("%a %-d %b · %-I:%M %p")
        lines.append(f"• {label}")
        buttons.append(InlineKeyboardButton(label, callback_data=f"clash:{i}"))

    keyboard = InlineKeyboardMarkup([
        buttons,
        [InlineKeyboardButton("Keep Original Time", callback_data="clash:keep")],
    ])
    await update.effective_message.reply_text(
        "\n".join(lines) + "\n\nPick a free slot or keep your original time:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )


async def _send_match_buttons(
    update: Update,
    matches: list[dict],
    prompt: str,
) -> None:
    """Send a list of matching events as inline buttons."""
    buttons = []
    for i, ev in enumerate(matches):
        day = ev["start"].strftime("%a %-d %b")
        time = ev["start"].strftime("%-I:%M %p")
        label = f"{ev['title']} · {day} {time}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"match:{i}")])
    keyboard = InlineKeyboardMarkup(buttons)
    await update.effective_message.reply_text(prompt, reply_markup=keyboard)


async def _send_calendar_pick_buttons(
    update: Update,
    title: str,
    calendars: list[dict],
) -> None:
    """Send calendar picker as inline buttons (2 per row)."""
    buttons = []
    row: list[InlineKeyboardButton] = []
    for i, cal in enumerate(calendars):
        row.append(InlineKeyboardButton(cal["name"], callback_data=f"cal:{i}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    keyboard = InlineKeyboardMarkup(buttons)
    await update.effective_message.reply_text(
        f"Which calendar should I add *{title}* to?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# Callback query handler (inline button presses)
# ---------------------------------------------------------------------------

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route all inline keyboard button presses."""
    query = update.callback_query
    await query.answer()  # dismiss the loading spinner

    user_id = update.effective_user.id
    data = query.data or ""

    try:
        await _dispatch_callback(update, context, user_id, data)
    except Exception as exc:
        logger.exception("Callback handler error for data=%r: %s", data, exc)
        try:
            await query.message.reply_text(f"❌ Something went wrong: {exc}")
        except Exception:
            pass


async def _dispatch_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    data: str,
) -> None:
    """Inner callback dispatcher — wrapped by handle_callback_query for error handling."""
    query = update.callback_query

    service, calendars = _get_service_and_calendars()
    if service is None:
        await query.edit_message_text("Google Calendar isn't connected. Use /start.")
        return

    session = session_memory.get(user_id)

    # --- Clash slot pick ---
    if data.startswith("clash:"):
        value = data.split(":", 1)[1]

        if value == "keep":
            session.pending_clash = False
            duration = session.duration_minutes or DEFAULT_DURATION_MINUTES
            if session.clash_pending_action == "create":
                event = calendar_service.create_event(
                    service, session.calendar_id, session.title,
                    session.start_dt, duration
                )
                day_str = _format_date_header(session.start_dt)
                time_str = session.start_dt.strftime("%-I:%M %p")
                await query.edit_message_text(
                    f"✅ Event created — *{event['title']}* on {day_str} at {time_str} · {session.calendar_name}",
                    parse_mode=ParseMode.MARKDOWN,
                )
                session_memory.record_action(
                    user_id, action="create", title=event["title"],
                    start_dt=session.start_dt, duration_minutes=duration,
                    calendar_id=session.calendar_id, calendar_name=session.calendar_name,
                    event_id=event["id"],
                )
            elif session.clash_pending_action == "edit":
                updated = calendar_service.update_event(
                    service, session.calendar_id, session.event_id,
                    start_dt=session.start_dt, duration_minutes=duration
                )
                day_str = _format_date_header(session.start_dt)
                time_str = session.start_dt.strftime("%-I:%M %p")
                await query.edit_message_text(
                    f"📝 Updated — *{updated['title']}* now on {day_str} at {time_str} · {session.calendar_name}",
                    parse_mode=ParseMode.MARKDOWN,
                )
            return

        slot_index = int(value)
        slots = session.clash_slots or []
        if slot_index >= len(slots):
            await query.edit_message_text("That slot is no longer available.")
            return

        chosen_start = slots[slot_index]
        duration = session.duration_minutes or DEFAULT_DURATION_MINUTES

        if session.clash_pending_action == "create":
            event = calendar_service.create_event(
                service, session.calendar_id, session.title, chosen_start, duration
            )
            day_str = _format_date_header(chosen_start)
            time_str = chosen_start.strftime("%-I:%M %p")
            await query.edit_message_text(
                f"✅ Event created — *{event['title']}* on {day_str} at {time_str} · {session.calendar_name}",
                parse_mode=ParseMode.MARKDOWN,
            )
            session_memory.record_action(
                user_id, action="create", title=event["title"],
                start_dt=chosen_start, duration_minutes=duration,
                calendar_id=session.calendar_id, calendar_name=session.calendar_name,
                event_id=event["id"],
            )
        elif session.clash_pending_action == "edit":
            updated = calendar_service.update_event(
                service, session.calendar_id, session.event_id,
                start_dt=chosen_start, duration_minutes=duration
            )
            day_str = _format_date_header(chosen_start)
            time_str = chosen_start.strftime("%-I:%M %p")
            await query.edit_message_text(
                f"📝 Updated — *{updated['title']}* now on {day_str} at {time_str} · {session.calendar_name}",
                parse_mode=ParseMode.MARKDOWN,
            )
            session_memory.record_action(
                user_id, action="edit", title=updated["title"],
                start_dt=chosen_start, duration_minutes=duration,
                calendar_id=session.calendar_id, calendar_name=session.calendar_name,
                event_id=updated["id"],
            )
        session.pending_clash = False
        return

    # --- Multi-match event pick ---
    if data.startswith("match:"):
        match_index = int(data.split(":", 1)[1])
        matches = session.pending_matches or []
        if match_index >= len(matches):
            await query.edit_message_text("That event is no longer available.")
            return

        event = matches[match_index]
        action = session.pending_match_action
        intent = session.pending_intent or {}
        session.pending_matches = []
        session.pending_match_action = None
        session.pending_intent = None

        # We need a real update object to pass to _apply_edit/_handle_delete
        # Patch the original message so downstream reply_text works
        if action == "edit":
            await query.edit_message_text(f"✏️ Editing *{event['title']}*...", parse_mode=ParseMode.MARKDOWN)
            await _apply_edit(update, user_id, intent, service, calendars, event)
        elif action == "delete":
            calendar_service.delete_event(service, event["calendar_id"], event["id"])
            day_str = event["start"].strftime("%a %-d %b")
            await query.edit_message_text(
                f"🗑️ Deleted — *{event['title']}* on {day_str}",
                parse_mode=ParseMode.MARKDOWN,
            )
            session_memory.clear(user_id)
        return

    # --- Calendar picker ---
    if data.startswith("cal:"):
        cal_index = int(data.split(":", 1)[1])
        cal_list = session.pending_calendar_list or []
        if cal_index >= len(cal_list):
            await query.edit_message_text("Calendar not found.")
            return

        chosen_cal = cal_list[cal_index]
        intent = session.pending_calendar_intent or {}
        session.pending_calendar_pick = False
        session.pending_calendar_list = []
        session.pending_calendar_intent = None

        start_dt = _resolve_datetime(intent)
        duration = intent.get("duration_minutes") or DEFAULT_DURATION_MINUTES
        intent["title"] = intent.get("title") or "New Event"

        await query.edit_message_text(f"📅 Adding to *{chosen_cal['name']}*...", parse_mode=ParseMode.MARKDOWN)
        await _handle_create(
            update, user_id, intent, service, calendars,
            start_dt, duration, chosen_cal["id"], chosen_cal["name"],
        )
        return

    # --- Appointment confirm/cancel ---
    if data.startswith("appt:"):
        value = data.split(":", 1)[1]
        booking = session.pending_appointment_booking

        if not booking:
            await query.edit_message_text("No pending appointment to confirm.")
            return

        if value == "yes":
            session.pending_appointment_booking = None
            appt_cal = calendar_service.find_appointments_calendar(calendars)
            start_dt = booking["start_dt"]
            duration = calendar_service.APPOINTMENT_DURATION_MINUTES

            if booking.get("placeholder_id"):
                try:
                    calendar_service.delete_event(service, appt_cal["id"], booking["placeholder_id"])
                except Exception:
                    pass

            event = calendar_service.create_event(
                service, appt_cal["id"],
                f"Appointment: {booking['client_name']}",
                start_dt, duration,
                color_id=calendar_service.COLOR_GREEN,
            )
            end_dt = start_dt + datetime.timedelta(minutes=duration)
            day_str = _format_date_header(start_dt)
            await query.edit_message_text(
                f"✅ Booked — *Appointment: {booking['client_name']}* on {day_str} "
                f"at {start_dt.strftime('%-I:%M %p')}–{end_dt.strftime('%-I:%M %p')} · Appointments 🟢",
                parse_mode=ParseMode.MARKDOWN,
            )
            session_memory.record_action(
                user_id, action="create", title=event["title"],
                start_dt=start_dt, duration_minutes=duration,
                calendar_id=appt_cal["id"], calendar_name=appt_cal["name"],
                event_id=event["id"],
            )

        elif value == "no":
            session.pending_appointment_booking = None
            await query.edit_message_text("Cancelled. Let me know when you'd like to book.")
        return


# ---------------------------------------------------------------------------
# Clash / multi-match resolution
# ---------------------------------------------------------------------------

async def _resolve_clash_pick(
    update: Update,
    user_id: int,
    choice: int,
    service,
    calendars: list[dict],
) -> None:
    """Handle 1/2/3 slot selection after a clash warning."""
    s = session_memory.get(user_id)
    slots = s.clash_slots

    if choice < 1 or choice > len(slots):
        await update.effective_message.reply_text(
            f"Please reply with a number between 1 and {len(slots)}."
        )
        return

    chosen_start = slots[choice - 1]
    duration = s.duration_minutes or DEFAULT_DURATION_MINUTES

    if s.clash_pending_action == "create":
        event = calendar_service.create_event(
            service, s.calendar_id, s.title, chosen_start, duration
        )
        day_str = _format_date_header(chosen_start)
        time_str = chosen_start.strftime("%-I:%M %p")
        await update.effective_message.reply_text(
            f"✅ Event created — *{event['title']}* on {day_str} at {time_str} · {s.calendar_name}",
            parse_mode=ParseMode.MARKDOWN,
        )
        session_memory.record_action(
            user_id,
            action="create",
            title=event["title"],
            start_dt=chosen_start,
            duration_minutes=duration,
            calendar_id=s.calendar_id,
            calendar_name=s.calendar_name,
            event_id=event["id"],
        )

    elif s.clash_pending_action == "edit":
        updated = calendar_service.update_event(
            service, s.calendar_id, s.event_id, start_dt=chosen_start, duration_minutes=duration
        )
        day_str = _format_date_header(chosen_start)
        time_str = chosen_start.strftime("%-I:%M %p")
        await update.effective_message.reply_text(
            f"📝 Updated — *{updated['title']}* now on {day_str} at {time_str} · {s.calendar_name}",
            parse_mode=ParseMode.MARKDOWN,
        )
        session_memory.record_action(
            user_id,
            action="edit",
            title=updated["title"],
            start_dt=chosen_start,
            duration_minutes=duration,
            calendar_id=s.calendar_id,
            calendar_name=s.calendar_name,
            event_id=updated["id"],
        )
    else:
        s.pending_clash = False


async def _resolve_match_pick(
    update: Update,
    user_id: int,
    choice: int,
    service,
    calendars: list[dict],
) -> None:
    """Handle numbered selection after multiple event matches."""
    s = session_memory.get(user_id)
    matches = s.pending_matches

    if choice < 1 or choice > len(matches):
        await update.effective_message.reply_text(
            f"Please reply with a number between 1 and {len(matches)}."
        )
        return

    event = matches[choice - 1]
    action = s.pending_match_action
    intent = s.pending_intent or {}

    # Clear pending state
    s.pending_matches = []
    s.pending_match_action = None
    s.pending_intent = None

    if action == "edit":
        await _apply_edit(update, user_id, intent, service, calendars, event)
    elif action == "delete":
        calendar_service.delete_event(service, event["calendar_id"], event["id"])
        day_str = event["start"].strftime("%a %-d %b")
        await update.effective_message.reply_text(
            f"🗑️ Deleted — *{event['title']}* on {day_str}",
            parse_mode=ParseMode.MARKDOWN,
        )
        session_memory.clear(user_id)


# ---------------------------------------------------------------------------
# Main message handler
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route all non-command text messages."""
    _save_chat_id(update.effective_chat.id)
    text = (update.message.text or "").strip()
    await _process_text(update, context, text)


_GREETINGS = {"hi", "hey", "hello", "helo", "hii", "yo", "sup", "heyyy", "heyy"}

async def _process_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Core routing logic — shared by text messages and voice transcriptions."""
    user_id = update.effective_user.id

    session = session_memory.get(user_id)
    session_memory.touch(user_id)

    # --- Greeting check ---
    if text.lower().strip().rstrip("!").rstrip("?") in _GREETINGS:
        creds = auth.load_credentials()
        if creds:
            await update.effective_message.reply_text(
                "Hi Nat! Ouni's on deck ✨ Ready when you are, what needs to go on the calendar?"
            )
        else:
            await update.effective_message.reply_text(
                "👋 Hey! I'm here but not connected to Google Calendar yet. Send /start to connect."
            )
        return

    # --- OAuth code exchange ---
    if session.pending_auth_flow is not None:
        try:
            creds = auth.exchange_code(session.pending_auth_flow, text)
            session.pending_auth_flow = None
            # Pre-load calendars
            global _calendars_cache
            svc = calendar_service.build_service(creds)
            _calendars_cache = calendar_service.list_calendars(svc)
            cal_names = ", ".join(c["name"] for c in _calendars_cache)
            await update.effective_message.reply_text(
                f"✅ Connected! Found {len(_calendars_cache)} calendar(s): {cal_names}\n\n"
                "Try: 'What do I have tomorrow?' or 'Add a meeting at 3pm'"
            )
        except Exception as exc:
            logger.error("OAuth exchange failed: %s", exc)
            await update.effective_message.reply_text(
                "❌ That code didn't work. Use /start to try again."
            )
        return

    # --- Require auth for everything else ---
    if not await _require_auth(update, user_id):
        return

    service, calendars = _get_service_and_calendars()
    if service is None:
        await update.effective_message.reply_text("Google Calendar isn't connected. Use /start.")
        return

    # Pending states are now handled via inline keyboard callbacks.
    # If the user types text while a pending state is active, route it as a new intent.

    # --- NLP intent parsing ---
    calendar_names = [c["name"] for c in calendars]
    today_str = datetime.date.today().isoformat()
    last_action_ctx = session_memory.to_context_dict(user_id)

    intent = intent_parser.parse_intent(text, calendar_names, last_action_ctx, today_str)
    action = intent.get("action", "unknown")

    if action == "unknown":
        await update.effective_message.reply_text(UNKNOWN_REPLY)
        return

    # --- Dispatch ---
    if action in ("create",):
        start_dt = _resolve_datetime(intent)
        if not start_dt:
            await update.effective_message.reply_text(
                "I need a date and time. Try: 'Add a meeting *tomorrow at 3pm*'"
            )
            return

        duration = intent.get("duration_minutes") or DEFAULT_DURATION_MINUTES
        hint = intent.get("calendar_hint")
        title = intent.get("title") or "New Event"
        intent["title"] = title

        # If unsure about calendar, ask before creating
        if hint == "ASK" or not hint:
            s = session_memory.get(user_id)
            s.pending_calendar_pick = True
            s.pending_calendar_list = calendars
            s.pending_calendar_intent = intent
            await _send_calendar_pick_buttons(update, title, calendars)
            return

        calendar_id = calendar_service.find_calendar_id(calendars, hint)
        calendar_name = next(
            (c["name"] for c in calendars if c["id"] == calendar_id), "Primary"
        )

        await _handle_create(
            update, user_id, intent, service, calendars,
            start_dt, duration, calendar_id, calendar_name,
        )

    elif action == "book_appointment":
        await _handle_book_appointment(update, user_id, intent, service, calendars)

    elif action == "edit":
        await _handle_edit(update, user_id, intent, service, calendars)

    elif action == "delete":
        await _handle_delete(update, user_id, intent, service, calendars)

    elif action == "view":
        await _handle_view(update, user_id, intent, service, calendars)

    elif action == "correct":
        await _handle_correct(update, user_id, intent, service, calendars)

    else:
        await update.effective_message.reply_text(UNKNOWN_REPLY)


# ---------------------------------------------------------------------------
# Voice note handler
# ---------------------------------------------------------------------------

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Transcribe a Telegram voice note via OpenAI Whisper, then route it
    through the same handle_message flow as a text message.
    """
    _save_chat_id(update.effective_chat.id)
    await update.effective_message.reply_text("🎙️ Transcribing...")

    voice = update.message.voice or update.message.audio
    tg_file = await context.bot.get_file(voice.file_id)

    # Download to a temp file with .ogg extension so Whisper recognises the format
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await tg_file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    try:
        with open(tmp_path, "rb") as audio_file:
            transcript = openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="en",
            )
        text = transcript.text.strip()
        if not text:
            await update.effective_message.reply_text("I couldn't make out what you said. Try again?")
            return

        logger.info("Voice transcribed: %s", text)
        await update.effective_message.reply_text(f'🎙️ _"{text}"_', parse_mode=ParseMode.MARKDOWN)

        # Process transcribed text directly through the message handling logic
        await _process_text(update, context, text)

    except Exception as exc:
        logger.error("Voice transcription failed: %s", exc)
        await update.effective_message.reply_text("❌ Couldn't transcribe that. Please try again or type it.")
    finally:
        import os
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Chat ID persistence (needed for proactive messages from background job)
# ---------------------------------------------------------------------------

def _save_chat_id(chat_id: int) -> None:
    try:
        with open(CHAT_ID_PATH, "w") as f:
            f.write(str(chat_id))
    except Exception as exc:
        logger.warning("Could not save chat_id: %s", exc)


def _load_chat_id() -> int | None:
    try:
        if os.path.exists(CHAT_ID_PATH):
            with open(CHAT_ID_PATH) as f:
                return int(f.read().strip())
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Notified follow-up tracker
# ---------------------------------------------------------------------------

def _load_notified() -> set[str]:
    try:
        if os.path.exists(NOTIFIED_FOLLOWUPS_PATH):
            with open(NOTIFIED_FOLLOWUPS_PATH) as f:
                return set(json.load(f))
    except Exception:
        pass
    return set()


def _save_notified(event_ids: set[str]) -> None:
    try:
        with open(NOTIFIED_FOLLOWUPS_PATH, "w") as f:
            json.dump(list(event_ids), f)
    except Exception as exc:
        logger.warning("Could not save notified followups: %s", exc)


# ---------------------------------------------------------------------------
# Post-appointment follow-up background job
# ---------------------------------------------------------------------------

async def _send_appt_followup(context) -> None:
    """One-shot job fired at the exact end time of an appointment."""
    event_id: str = context.job.data["event_id"]
    event_title: str = context.job.data["event_title"]
    chat_id: int = context.job.data["chat_id"]

    notified = _load_notified()
    if event_id in notified:
        return  # already sent (e.g. bot restarted and re-scheduled)

    client_name = event_title.removeprefix("Appointment:").strip()
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"Hey Nat! Your *Appointment: {client_name}* just wrapped up ✅\n\n"
                "What was this appointment about? And any to-dos to capture?"
            ),
            parse_mode="Markdown",
        )
        notified.add(event_id)
        _save_notified(notified)
        logger.info("Sent follow-up for appointment: %s", event_title)
    except Exception as exc:
        logger.error("Failed to send follow-up for %s: %s", event_title, exc)


async def _schedule_todays_followups(context) -> None:
    """
    Runs once at 8am SGT each day. Fetches today's confirmed appointments
    and schedules a one-shot follow-up message for each one's end time.
    """
    chat_id = _load_chat_id()
    if not chat_id:
        logger.info("Daily appt scan: no chat_id saved yet, skipping.")
        return

    creds = auth.load_credentials()
    if not creds:
        logger.info("Daily appt scan: not authenticated, skipping.")
        return

    try:
        service = calendar_service.build_service(creds)
        calendars = calendar_service.list_calendars(service)
        appt_cal = calendar_service.find_appointments_calendar(calendars)
        if not appt_cal:
            return

        today = datetime.date.today()
        events = calendar_service.get_todays_confirmed_appointments(service, appt_cal, today)
    except Exception as exc:
        logger.error("Daily appt scan failed: %s", exc)
        return

    if not events:
        logger.info("Daily appt scan: no confirmed appointments today.")
        return

    notified = _load_notified()
    now = datetime.datetime.now(tz=TZ)
    scheduled = 0

    for event in events:
        if event["id"] in notified:
            continue  # already followed up on a previous day

        fire_at = event["end"]
        if fire_at <= now:
            continue  # already ended before the morning scan ran

        context.job_queue.run_once(
            _send_appt_followup,
            when=fire_at,
            data={
                "event_id": event["id"],
                "event_title": event["title"],
                "chat_id": chat_id,
            },
            name=f"followup_{event['id']}",
        )
        client_name = event["title"].removeprefix("Appointment:").strip()
        logger.info("Scheduled follow-up for '%s' at %s", client_name, fire_at.strftime("%-I:%M %p"))
        scheduled += 1

    logger.info("Daily appt scan: scheduled %d follow-up(s) for today.", scheduled)


# ---------------------------------------------------------------------------
# App bootstrap
# ---------------------------------------------------------------------------

def main() -> None:
    """Build and run the Telegram bot (polling mode)."""
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))

    # Post-appointment follow-up: scan calendar once at 8am SGT each day,
    # then schedule precise one-shot messages at each appointment's end time.
    app.job_queue.run_daily(
        _schedule_todays_followups,
        time=datetime.time(hour=8, minute=0, tzinfo=TZ),
    )
    # Also run at startup in case the bot restarted after 8am today
    app.job_queue.run_once(_schedule_todays_followups, when=10)

    logger.info("Bot started — polling for updates...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
