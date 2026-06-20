"""Call-for-Papers detection and deadline-reminder state.

Standard-library only — keep it import-light so unit tests need no
network or API dependencies.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

# Plain case-insensitive substring phrases (incl. common typos).
CFP_SUBSTRINGS = [
    "call for papers",
    "call for paper",
    "call for papres",
    "call for papre",
]
# Short tokens that need word-boundary guards to avoid matching inside
# longer words or URL fragments.
CFP_BOUNDARY_WORDS = ["cfp", "call4papers"]
_BOUNDARY_RE = re.compile(
    r"(?<![a-z0-9])(?:" + "|".join(CFP_BOUNDARY_WORDS) + r")(?![a-z0-9])",
    re.IGNORECASE,
)


def is_call_for_papers(text: str) -> bool:
    """True if the post text looks like a Call for Papers."""
    if not text:
        return False
    low = text.lower()
    if any(sub in low for sub in CFP_SUBSTRINGS):
        return True
    return bool(_BOUNDARY_RE.search(low))


REMINDER_FILE_NAME = "cfp_reminders.json"


def _reminder_path(state_dir: Path) -> Path:
    return Path(state_dir) / REMINDER_FILE_NAME


def _load_reminders(state_dir: Path) -> list[dict]:
    path = _reminder_path(state_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_reminders(state_dir: Path, records: list[dict]) -> None:
    path = _reminder_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")


def record_deadlines(state_dir: Path, events: list[dict], calendar_urls: list) -> int:
    """Append CfP deadline records for later Telegram reminders.

    Records one entry per event with classification 'call_for_papers' and a
    truthy 'date'. Dedupes by post_id. Returns the count newly recorded.
    """
    records = _load_reminders(state_dir)
    known = {r.get("post_id") for r in records}
    added = 0
    for event, url in zip(events, calendar_urls):
        if event.get("classification") != "call_for_papers":
            continue
        deadline = event.get("date")
        post_id = event.get("post_id")
        if not deadline or not post_id or post_id in known:
            continue
        records.append({
            "post_id": post_id,
            "title": event.get("title") or "Call for Papers",
            "deadline": deadline,
            "calendar_url": url,
            "reminded": False,
        })
        known.add(post_id)
        added += 1
    if added:
        _save_reminders(state_dir, records)
    return added


def due_reminders(state_dir: Path, today: date, within_days: int = 14) -> list[dict]:
    """Return CfP records whose deadline is within `within_days` and not yet
    reminded; mark them reminded, prune past deadlines, and persist.
    """
    records = _load_reminders(state_dir)
    if not records:
        return []

    kept: list[dict] = []
    due: list[dict] = []
    changed = False
    for rec in records:
        try:
            deadline = date.fromisoformat(rec["deadline"])
        except (KeyError, ValueError):
            continue  # drop malformed records
        days_left = (deadline - today).days
        if days_left < 0:
            changed = True            # past -> prune (don't keep)
            continue
        if days_left <= within_days and not rec.get("reminded"):
            rec["reminded"] = True
            changed = True
            due.append({**rec, "days_left": days_left})
        kept.append(rec)

    if changed:
        _save_reminders(state_dir, kept)
    return due
