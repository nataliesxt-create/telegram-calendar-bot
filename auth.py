"""
Google OAuth 2.0 flow for a single user.

Token is persisted in two places:
  1. token.json on disk (for local dev)
  2. GOOGLE_TOKEN_JSON env var on Railway (survives redeploys)

On save, both are updated. On load, env var takes priority over file.
"""

import json
import os

import httpx
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
    Persist credentials to token.json and to the Railway GOOGLE_TOKEN_JSON env var
    via the Railway API (works inside the container, no CLI needed).
    """
    token_data = creds.to_json()

    # Write to disk for local dev fallback
    with open(TOKEN_PATH, "w") as f:
        f.write(token_data)

    # Push to Railway via GraphQL API so it survives redeploys
    _push_to_railway(token_data)


def _push_to_railway(token_data: str) -> None:
    """Update GOOGLE_TOKEN_JSON env var on Railway via their API."""
    import logging
    logger = logging.getLogger(__name__)

    api_token = os.environ.get("RAILWAY_API_TOKEN")
    service_id = os.environ.get("RAILWAY_SERVICE_ID")
    environment_id = os.environ.get("RAILWAY_ENVIRONMENT_ID")

    if not all([api_token, service_id, environment_id]):
        logger.warning("Railway env vars missing — token not persisted. API_TOKEN=%s SERVICE_ID=%s ENV_ID=%s",
                       bool(api_token), bool(service_id), bool(environment_id))
        return

    query = """
    mutation upsertVariables($input: VariableCollectionUpsertInput!) {
      variableCollectionUpsert(input: $input)
    }
    """
    variables = {
        "input": {
            "serviceId": service_id,
            "environmentId": environment_id,
            "variables": {"GOOGLE_TOKEN_JSON": token_data},
        }
    }

    try:
        resp = httpx.post(
            "https://backboard.railway.com/graphql/v2",
            json={"query": query, "variables": variables},
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=10,
        )
        if resp.status_code == 200 and "errors" not in resp.json():
            logger.info("Google token persisted to Railway env var successfully.")
        else:
            logger.error("Railway API returned error: %s", resp.text)
    except Exception as exc:
        logger.error("Failed to persist token to Railway: %s", exc)
