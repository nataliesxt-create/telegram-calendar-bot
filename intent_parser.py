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
  "action": "create | edit | delete | view | correct | unknown",
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
- duration_minutes: parse "30 min", "2 hours", "1.5 hours" etc. Default null if absent.
- calendar_hint: infer from context (e.g. "standup" → Work, "dentist" → Personal,
  "lash appointment" → Personal/Self Care, "lunch with mum" → Personal). Use exact
  calendar names from the provided list when possible. Null if genuinely unclear.
- is_correction: true when the user is amending their immediately previous instruction
  (phrases like "actually", "wait", "no make it", "change to", "instead").
- action "correct": use when the user's message is clearly modifying the last action
  (e.g. "actually make it 4pm", "add it to Work instead").
- action "view": for queries about what's scheduled ("what's on", "show me", "do I
  have anything").
- action "create": use when the user mentions any appointment, event, or activity with
  a date/time — even without explicit words like "add" or "book". For example,
  "lash appointment with Jermsy Beauty on 22nd June at 8AM" is a create action.
- action "unknown": ONLY when intent is completely unclear with no date or event mentioned.
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
