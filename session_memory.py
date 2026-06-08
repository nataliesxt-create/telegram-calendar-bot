"""
Short-term per-user session memory.

Stores the last calendar action so that correction messages like
"actually make it 4pm" can be applied without the user repeating context.

Memory expires after SESSION_TIMEOUT_MINUTES of inactivity.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from config import SESSION_TIMEOUT_MINUTES, TZ


@dataclass
class SessionState:
    """Holds state for one user session."""

    # Last resolved action
    action: str | None = None
    title: str | None = None
    date: datetime.date | None = None
    start_dt: datetime.datetime | None = None
    duration_minutes: int | None = None
    calendar_id: str | None = None
    calendar_name: str | None = None
    event_id: str | None = None

    # Pending clash resolution
    pending_clash: bool = False
    clash_slots: list[datetime.datetime] = field(default_factory=list)
    clash_pending_action: str | None = None  # "create" or "edit"

    # Pending multi-match selection
    pending_matches: list[dict] = field(default_factory=list)
    pending_match_action: str | None = None  # "edit" or "delete"
    pending_intent: dict | None = None  # original intent for edit/delete

    # Auth flow
    pending_auth_flow: object = None  # holds the Flow object during OAuth

    # Timestamp of last activity
    last_active: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(tz=TZ)
    )


class SessionMemory:
    """In-memory store for per-user sessions with automatic expiry."""

    def __init__(self) -> None:
        self._store: dict[int, SessionState] = {}

    def get(self, user_id: int) -> SessionState:
        """
        Return the session for `user_id`, creating a fresh one if needed.

        Expired sessions are silently reset before returning.
        """
        session = self._store.get(user_id)
        if session is None:
            session = SessionState()
            self._store[user_id] = session
            return session

        now = datetime.datetime.now(tz=TZ)
        age = (now - session.last_active).total_seconds() / 60
        if age > SESSION_TIMEOUT_MINUTES:
            session = SessionState()
            self._store[user_id] = session

        return session

    def touch(self, user_id: int) -> None:
        """Update the last-active timestamp for a user."""
        session = self._store.get(user_id)
        if session:
            session.last_active = datetime.datetime.now(tz=TZ)

    def clear(self, user_id: int) -> None:
        """Reset session state for a user (keeps the slot, resets fields)."""
        self._store[user_id] = SessionState()

    def record_action(
        self,
        user_id: int,
        *,
        action: str,
        title: str | None = None,
        start_dt: datetime.datetime | None = None,
        duration_minutes: int | None = None,
        calendar_id: str | None = None,
        calendar_name: str | None = None,
        event_id: str | None = None,
    ) -> None:
        """
        Save the result of a completed calendar action to session memory.

        Also clears any pending clash / match state.
        """
        s = self.get(user_id)
        s.action = action
        s.title = title
        s.start_dt = start_dt
        s.date = start_dt.date() if start_dt else None
        s.duration_minutes = duration_minutes
        s.calendar_id = calendar_id
        s.calendar_name = calendar_name
        s.event_id = event_id
        s.pending_clash = False
        s.clash_slots = []
        s.clash_pending_action = None
        s.pending_matches = []
        s.pending_match_action = None
        s.pending_intent = None
        s.last_active = datetime.datetime.now(tz=TZ)

    def to_context_dict(self, user_id: int) -> dict | None:
        """
        Return a JSON-serialisable summary of the last action for Claude's context.

        Returns None if no action has been recorded yet.
        """
        s = self.get(user_id)
        if not s.action:
            return None
        return {
            "action": s.action,
            "title": s.title,
            "date": s.date.isoformat() if s.date else None,
            "start_time": s.start_dt.strftime("%H:%M") if s.start_dt else None,
            "duration_minutes": s.duration_minutes,
            "calendar_name": s.calendar_name,
            "event_id": s.event_id,
        }
