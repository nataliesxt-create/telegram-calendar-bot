"""
Google OAuth 2.0 flow for a single user.

Token is persisted in two places:
  1. token.json on disk (for local dev)
  2. GOOGLE_TOKEN_JSON env var on Railway (survives redeploys)

On save, both are updated. On load, env var takes priority over file.
"""

import json
import os
import subprocess

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from config import (
    CREDENTIALS_PATH,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    GOOGLE_SCOPES,
    OAUTH_REDIRECT_URI,
    TOKEN_PATH,
)


def _client_config() -> dict:
    """Build the OAuth client config dict from env vars or credentials.json."""
    if os.path.exists(CREDENTIALS_PATH):
        with open(CREDENTIALS_PATH) as f:
            return json.load(f)
    return {
        "installed": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [OAUTH_REDIRECT_URI],
        }
    }


def get_auth_url() -> tuple[str, Flow]:
    """
    Generate a Google OAuth URL for the user to visit.

    Returns:
        (auth_url, flow) — send the URL to the user; pass flow to
        exchange_code() once they reply with the code.
    """
    flow = Flow.from_client_config(
        _client_config(),
        scopes=GOOGLE_SCOPES,
        redirect_uri=OAUTH_REDIRECT_URI,
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return auth_url, flow


def exchange_code(flow: Flow, code: str) -> Credentials:
    """
    Exchange the authorization code for credentials and persist them.

    Args:
        flow:  The Flow object returned by get_auth_url().
        code:  The code the user received after authorising.

    Returns:
        Google Credentials object (also saved to disk + Railway env var).
    """
    flow.fetch_token(code=code)
    creds = flow.credentials
    _save_token(creds)
    return creds


def load_credentials() -> Credentials | None:
    """
    Load credentials, preferring the GOOGLE_TOKEN_JSON env var over token.json.

    Auto-refreshes expired tokens and re-persists them.

    Returns:
        Valid Credentials, or None if the user hasn't authenticated yet.
    """
    token_json = os.environ.get("GOOGLE_TOKEN_JSON")

    if token_json:
        try:
            creds = Credentials.from_authorized_user_info(
                json.loads(token_json), GOOGLE_SCOPES
            )
        except Exception:
            creds = None
    elif os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, GOOGLE_SCOPES)
    else:
        return None

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_token(creds)

    return creds if creds and creds.valid else None


def _save_token(creds: Credentials) -> None:
    """
    Persist credentials to token.json and to the Railway GOOGLE_TOKEN_JSON env var.

    The Railway CLI call is best-effort — it won't crash the bot if it fails
    (e.g. running locally without railway CLI).
    """
    token_data = creds.to_json()

    # Always write to disk (works locally and gives Railway a fallback)
    with open(TOKEN_PATH, "w") as f:
        f.write(token_data)

    # Also push to Railway env var so it survives redeploys
    try:
        subprocess.run(
            ["railway", "variables", "--set", f"GOOGLE_TOKEN_JSON={token_data}"],
            capture_output=True,
            timeout=15,
        )
    except Exception:
        pass  # Local dev without railway CLI — that's fine
