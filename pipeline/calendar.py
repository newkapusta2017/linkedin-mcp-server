"""
Google Calendar integration for the saved-posts pipeline.

Creates calendar events from classified LinkedIn posts.  Uses OAuth2
for user-level access — the first run opens a browser for consent,
then caches the token locally for subsequent runs.

Required setup:
1. Create a Google Cloud project and enable the Calendar API.
2. Create OAuth 2.0 credentials (Desktop app type).
3. Download the client secrets JSON file.
4. Set GOOGLE_CREDENTIALS_FILE to that path (or place it at
   ~/.linkedin-mcp/pipeline/credentials.json).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

DEFAULT_STATE_DIR = Path.home() / ".linkedin-mcp" / "pipeline"
DEFAULT_TOKEN_PATH = DEFAULT_STATE_DIR / "token.json"
DEFAULT_CREDENTIALS_PATH = DEFAULT_STATE_DIR / "credentials.json"


def _get_credentials(
    credentials_file: Path | None = None,
    token_file: Path | None = None,
) -> Credentials:
    """Load or create OAuth2 credentials for Google Calendar.

    Tries in order:
    1. Cached token from a previous run.
    2. Application Default Credentials (``gcloud auth application-default login``).
    3. OAuth2 client secrets file (opens browser for consent).
    """
    credentials_file = credentials_file or Path(
        os.environ.get("GOOGLE_CREDENTIALS_FILE", str(DEFAULT_CREDENTIALS_PATH))
    )
    token_file = token_file or DEFAULT_TOKEN_PATH

    creds: Credentials | None = None

    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        logger.info("Refreshing expired Google token")
        creds.refresh(Request())
        token_file.write_text(creds.to_json(), encoding="utf-8")
        return creds

    # Try Application Default Credentials (from gcloud auth)
    try:
        import google.auth

        adc, _ = google.auth.default(scopes=SCOPES)
        if adc:
            logger.info("Using Application Default Credentials (gcloud)")
            return adc
    except Exception:
        pass

    # Try access token from env var (set via: gcloud auth print-access-token)
    token = os.environ.get("GOOGLE_ACCESS_TOKEN", "")
    if token and len(token) > 20:
        logger.info("Using GOOGLE_ACCESS_TOKEN from environment")
        return Credentials(token=token)

    if not credentials_file.exists():
        raise FileNotFoundError(
            f"No Google credentials found. Either run:\n"
            f"  gcloud auth application-default login "
            f"--scopes=https://www.googleapis.com/auth/cloud-platform,"
            f"https://www.googleapis.com/auth/calendar.events\n"
            f"Or download OAuth2 client secrets to {credentials_file}"
        )
    logger.info("Starting OAuth consent flow — a browser window will open")
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
    creds = flow.run_local_server(port=0)

    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json(), encoding="utf-8")
    logger.info("Google token saved to %s", token_file)

    return creds


def _get_service(
    credentials_file: Path | None = None,
    token_file: Path | None = None,
) -> Any:
    """Build an authorized Google Calendar API service object."""
    creds = _get_credentials(credentials_file, token_file)
    return build("calendar", "v3", credentials=creds)


def _build_event_body(classification: dict[str, Any]) -> dict[str, Any]:
    """Convert a classification dict into a Google Calendar event body."""
    title = classification.get("title") or "LinkedIn Event"
    description = classification.get("description") or ""
    location = classification.get("location")
    date_str = classification.get("date")
    start_time = classification.get("start_time")
    end_time = classification.get("end_time")
    post_id = classification.get("post_id", "")

    if description and post_id:
        description += f"\n\n[pipeline post_id: {post_id}]"

    event: dict[str, Any] = {
        "summary": title,
        "description": description,
    }

    if location:
        event["location"] = location

    if date_str and start_time:
        start_dt = datetime.fromisoformat(f"{date_str}T{start_time}:00")
        if end_time:
            end_dt = datetime.fromisoformat(f"{date_str}T{end_time}:00")
        else:
            end_dt = start_dt + timedelta(hours=1)
        event["start"] = {"dateTime": start_dt.isoformat(), "timeZone": "Europe/Berlin"}
        event["end"] = {"dateTime": end_dt.isoformat(), "timeZone": "Europe/Berlin"}
    elif date_str:
        event["start"] = {"date": date_str}
        event["end"] = {"date": date_str}
    else:
        today = datetime.now().strftime("%Y-%m-%d")
        event["start"] = {"date": today}
        event["end"] = {"date": today}

    return event


def create_event(
    service: Any,
    classification: dict[str, Any],
    calendar_id: str | None = None,
) -> dict[str, Any]:
    """Create a single Google Calendar event from a classification result.

    Args:
        service: An authorized Google Calendar API service object.
        classification: Dict from classify_post with event details.
        calendar_id: Target calendar. Defaults to ``GOOGLE_CALENDAR_ID``
            env var or ``"primary"``.

    Returns:
        The created event resource from the API.
    """
    calendar_id = calendar_id or os.environ.get("GOOGLE_CALENDAR_ID", "primary")
    body = _build_event_body(classification)

    logger.info(
        "Creating calendar event: %s on %s",
        body.get("summary"),
        body.get("start", {}).get("date") or body.get("start", {}).get("dateTime"),
    )

    event = service.events().insert(calendarId=calendar_id, body=body).execute()
    logger.info("Created event: %s", event.get("htmlLink"))
    return event


def create_events(
    classifications: list[dict[str, Any]],
    credentials_file: Path | None = None,
    token_file: Path | None = None,
    calendar_id: str | None = None,
) -> list[dict[str, Any]]:
    """Create calendar events for all event-type classifications.

    Filters out classifications where ``classification == "none"`` and
    creates a Google Calendar event for each remaining item.

    Returns:
        List of created event resources from the API.
    """
    events_to_create = [c for c in classifications if c.get("classification") != "none"]

    if not events_to_create:
        logger.info("No events to create (all posts classified as none)")
        return []

    logger.info("Creating %d calendar event(s)", len(events_to_create))
    service = _get_service(credentials_file, token_file)

    created: list[dict[str, Any]] = []
    for classification in events_to_create:
        try:
            event = create_event(service, classification, calendar_id)
            created.append(event)
        except Exception as exc:
            logger.error(
                "Failed to create event for post %s: %s",
                classification.get("post_id", "unknown"),
                exc,
            )

    logger.info("Created %d of %d calendar events", len(created), len(events_to_create))
    return created
