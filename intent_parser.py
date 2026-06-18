"""
NLP intent parsing via OpenAI GPT-4o-mini.

Sends each user message to OpenAI with a structured system prompt and
returns a parsed intent dict.
"""

from __future__ import annotations

import json
import logging
import re

from openai import OpenAI

from config import OPENAI_API_KEY

logger = logging.getLogger(__name__)

_client = OpenAI(api_key=OPENAI_API_KEY)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a calendar assistant intent parser. Given a user message about their calendar,
extract structured intent as JSON.

You will be given:
- The user's message
- A list of their calendar names
- The last action context (may be null)

Return ONLY valid JSON matching this schema exactly:
{
  "action": "create | book_appointment | edit | delete | view | correct | unknown",
  "title": "string or null",
  "date": "YYYY-MM-DD or null",
  "time": "HH:MM in 24h format or null",
  "duration_minutes": integer or null,
  "calendar_hint": "calendar name or null",
  "is_correction": true or false,
  "original_message": "the user's original message verbatim"
}

Rules:
- For relative dates (today/tomorrow/Monday/next week/etc.), resolve against the
  provided current date. Output as YYYY-MM-DD.
- For times like "3pm" output "15:00", "10am" → "10:00".
- For ambiguous time-of-day words (morning/afternoon/evening/night), set time to null
  and let the application resolve them — do NOT guess.
- duration_minutes: parse "30 min", "2 hours", "1.5 hours" etc.
  If action is "book_appointment" OR calendar_hint is "Appointments", default to 90 minutes.
  For all other events default null (app will use its own default).
- calendar_hint: infer from context using these strict rules in order:
  1. "Self Care + Activities": user is doing something for themselves —
     lash, nails, facial, massage, salon, spa, brow, wax, blowout, hair,
     dentist, doctor, physio, therapy, clinic, dermatologist, GP, optician,
     gym, workout, yoga, pilates, run, hike, sports, hobby, activity.
  2. "Appointments": ONLY when user is the SERVICE PROVIDER booking a client in —
     "appointment with [client name]", "book [name] in", "slot for [name]".
  3. "West Family": any event mentioning Ellie, Chris, or family.
  4. "branding dept": anything branding-related.
  5. "Roadshows": roadshow events.
  6. "Travels + Work Trips": travel, flights, trips, hotels, overseas.
  7. "Social Media & Content": ONLY explicit content production — filming, photoshoot, recording, content shoot, blog shoot. NOT meals, restaurants, social gatherings, or events at a venue.
  8. "Birthdays": birthdays.
  9. "Routines": routines, habits, daily recurring tasks.
  10. "Commute and Activities": commute, school run, picking up/dropping off.
  11. "Growth-centric": courses, learning, books, workshops, personal development, seminars.
  12. "Bloom Into Legacy": anything related to Bloom Into Legacy brand/business.
  13. "Champagne & Chaos": anything related to Champagne & Chaos brand/business.
  14. "Work": construction-related — architects, HDB, MOE, contractors, authorities,
      site visits, permits, renovation, building works.
  15. "Team Meetings + Events": ONLY when user explicitly says "team".
  16. "Agency Events + Meetings": company/agency-wide events — congresses, boost events,
      company launches, Great Eastern, Craigasabel, agency-level gatherings.
  17. "ASK": if none of the above 16 rules apply clearly. Set calendar_hint="ASK" —
      do NOT change the action to "unknown". Common cases: meals, dinner, lunch,
      breakfast, coffee, catch-ups, social events with friends, restaurant names.
      The action is still "create" — only the calendar is uncertain.
- is_correction: true when the user is amending their immediately previous instruction
  (phrases like "actually", "wait", "no make it", "change to", "instead").
- action "correct": use when the user's message is clearly modifying the last action.
- action "view": for queries about what's scheduled ("what's on", "show me", "do I have anything").
- action "book_appointment": ONLY when the user is booking a named client/person into
  their Appointments calendar (they are the provider, not the recipient). Examples:
  "appointment with Nicholas", "book Sarah in for Thursday", "slot for Michael at 2pm".
  Title = client name only (e.g. "Nicholas", "Sarah"). Date/time optional.
- action "create": all other event creation — personal, work, self-care, social.
- action "unknown": ONLY when intent is completely unclear.
- Return ONLY the JSON object, no explanation, no markdown fences.
"""


def parse_intent(
    user_message: str,
    calendar_names: list[str],
    last_action: dict | None,
    today_str: str,
) -> dict:
    """
    Parse a user message into a structured intent dict.

    Args:
        user_message:   Raw text from the Telegram user.
        calendar_names: List of calendar display names to help with inference.
        last_action:    The last session action dict (or None).
        today_str:      Today's date as YYYY-MM-DD for relative date resolution.

    Returns:
        Parsed intent dict.  If parsing fails, returns an "unknown" action dict.
    """
    context_block = f"""
Current date: {today_str}
User's calendars: {", ".join(calendar_names) if calendar_names else "Primary"}
Last action context: {json.dumps(last_action) if last_action else "none"}

User message: {user_message}
""".strip()

    try:
        response = _client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=512,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": context_block},
            ],
        )
        raw = response.choices[0].message.content.strip()

        # Strip markdown fences if model wrapped them anyway
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        intent = json.loads(raw)
        intent.setdefault("original_message", user_message)
        logger.info("Intent parsed: action=%s calendar_hint=%s title=%s",
                    intent.get("action"), intent.get("calendar_hint"), intent.get("title"))
        return intent

    except (json.JSONDecodeError, IndexError, Exception) as exc:
        logger.error("Intent parsing failed: %s", exc)
        return {
            "action": "unknown",
            "title": None,
            "date": None,
            "time": None,
            "duration_minutes": None,
            "calendar_hint": None,
            "is_correction": False,
            "original_message": user_message,
        }


UNKNOWN_REPLY = (
    "I didn't catch that. Try:\n"
    "• 'Add a meeting tomorrow at 3pm'\n"
    "• 'What's on Friday?'\n"
    "• 'Move my 3pm call to 5pm'\n"
    "• 'Cancel my dentist on Friday'"
)
