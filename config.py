"""Central configuration — all constants and env var loading."""

import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# Timezone
TIMEZONE = "Asia/Singapore"
TZ = ZoneInfo(TIMEZONE)

# Calendar defaults
DEFAULT_DURATION_MINUTES = 60
LOOKAHEAD_DAYS = 365
DEFAULT_CALENDAR = "primary"

# Ambiguous time-of-day defaults (24h)
TIME_DEFAULTS = {
    "morning": "09:00",
    "afternoon": "14:00",
    "evening": "19:00",
    "night": "21:00",
}

# Calendars to exclude from clash detection (noise/shared calendars)
CLASH_EXCLUDED_CALENDARS = {
    "pfgroadshow@gmail.com",
    "sc direct group",
    "team meeting",
}

# Session expiry
SESSION_TIMEOUT_MINUTES = 30

# Google OAuth scopes
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
]

# File paths
TOKEN_PATH = "token.json"
CREDENTIALS_PATH = "credentials.json"  # downloaded from Google Cloud Console

# Secrets from environment
TELEGRAM_TOKEN: str = os.environ["TELEGRAM_TOKEN"]
GOOGLE_CLIENT_ID: str = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET: str = os.environ["GOOGLE_CLIENT_SECRET"]
OPENAI_API_KEY: str = os.environ["OPENAI_API_KEY"]

# OAuth redirect URI (must match Google Cloud Console)
OAUTH_REDIRECT_URI: str = os.getenv("OAUTH_REDIRECT_URI", "urn:ietf:wg:oauth:2.0:oob")
