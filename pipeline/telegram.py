"""
Telegram notifications for the LinkedIn pipeline.

Sends event notifications and handles interactive date input
when the classifier finds an event but can't extract a date.

Requires a dedicated bot token (LINKEDIN_BOT_TOKEN) separate from
the job-checker bot, since both use getUpdates polling.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

STATE_DIR = Path.home() / ".linkedin-mcp" / "pipeline"
PENDING_FILE = STATE_DIR / "pending_events.json"
OFFSET_FILE = STATE_DIR / "telegram_offset.json"


def _token():
    return os.environ.get("LINKEDIN_BOT_TOKEN")


def _chat_id():
    return os.environ.get("LINKEDIN_CHAT_ID")


def _reply_chat_id():
    """DM chat for interactive prompts (replies don't work in channels)."""
    return os.environ.get("LINKEDIN_REPLY_CHAT_ID") or _chat_id()


def _api(method, **params):
    token = _token()
    if not token:
        return None
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
        if not result.get("ok"):
            logger.error("Telegram API error: %s", result)
            return None
        return result.get("result")
    except (URLError, OSError) as exc:
        logger.error("Telegram request failed: %s", exc)
        return None


def notify_created(event, calendar_url=None, chat_id=None):
    """Send notification for a successfully created calendar event."""
    chat_id = chat_id or _chat_id()
    if not chat_id or not _token():
        return

    title = event.get("title") or "LinkedIn Event"
    date = event.get("date") or "no date"
    t = ""
    if event.get("start_time"):
        t = f" {event['start_time']}"
        if event.get("end_time"):
            t += f"–{event['end_time']}"
    location = event.get("location") or ""

    text = f"✅ <b>{title}</b>\n\U0001f4c5 {date}{t}"
    if location:
        text += f"\n\U0001f4cd {location}"
    if calendar_url:
        text += f'\n\U0001f517 <a href="{calendar_url}">Open in Calendar</a>'

    _api("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")


def notify_deadline_reminder(record, chat_id=None):
    """Telegram reminder that a CfP submission deadline is approaching."""
    chat_id = chat_id or _chat_id()
    if not chat_id or not _token():
        return

    title = record.get("title") or "Call for Papers"
    deadline = record.get("deadline") or "?"
    days_left = record.get("days_left")
    when = f"in {days_left} day(s)" if days_left is not None else "soon"

    text = f"⏰ <b>CfP deadline {when}</b>\n{title}\n\U0001f4c5 {deadline}"
    if record.get("calendar_url"):
        text += f'\n\U0001f517 <a href="{record["calendar_url"]}">Open in Calendar</a>'

    _api("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")


def notify_missing_date(event, chat_id=None, pending_file=None):
    """Send notification for an event without a date, asking user to reply."""
    chat_id = chat_id or _reply_chat_id()
    if not chat_id or not _token():
        return

    title = event.get("title") or "LinkedIn Event"
    desc = (event.get("description") or "")[:300]

    text = (
        f"⚠️ <b>{title}</b>\n"
        f"Event found but no date extracted.\n\n"
        f"<i>{desc}</i>\n\n"
        f"Reply with the date to create a calendar event:\n"
        f"<code>03.12.2026</code> or <code>03.12.2026 17:30</code>"
    )

    result = _api(
        "sendMessage",
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup={"force_reply": True, "selective": True},
    )

    if result:
        _save_pending(result["message_id"], event, pending_file)
        logger.info("Pending event stored (msg_id=%s)", result["message_id"])


def _save_pending(message_id, event, pending_file=None):
    pf = pending_file or PENDING_FILE
    pending = _load_pending(pf)
    pending[str(message_id)] = event
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(json.dumps(pending, ensure_ascii=False), encoding="utf-8")


def _load_pending(pending_file=None):
    pf = pending_file or PENDING_FILE
    if not pf.exists():
        return {}
    try:
        return json.loads(pf.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _remove_pending(message_id, pending_file=None):
    pf = pending_file or PENDING_FILE
    pending = _load_pending(pf)
    pending.pop(str(message_id), None)
    pf.write_text(json.dumps(pending, ensure_ascii=False), encoding="utf-8")


def _load_offset():
    if not OFFSET_FILE.exists():
        return None
    try:
        return json.loads(OFFSET_FILE.read_text(encoding="utf-8")).get("offset")
    except (json.JSONDecodeError, OSError):
        return None


def _save_offset(offset):
    OFFSET_FILE.write_text(json.dumps({"offset": offset}), encoding="utf-8")


def _parse_user_date(text):
    """Parse date from user input: DD.MM.YYYY [HH:MM[-HH:MM]]"""
    m = re.match(
        r"(\d{1,2})[./](\d{1,2})[./](\d{4})"
        r"(?:\s+(\d{1,2})[:.:](\d{2})"
        r"(?:\s*[-–]+\s*(\d{1,2})[:.:](\d{2}))?)?",
        text.strip(),
    )
    if not m:
        return None

    day = m.group(1).zfill(2)
    month = m.group(2).zfill(2)
    year = m.group(3)
    result = {"date": f"{year}-{month}-{day}"}

    if m.group(4) and m.group(5):
        result["start_time"] = f"{m.group(4).zfill(2)}:{m.group(5)}"
    if m.group(6) and m.group(7):
        result["end_time"] = f"{m.group(6).zfill(2)}:{m.group(7)}"

    return result


def process_replies():
    """Check for replies to pending messages. Returns events ready for calendar."""
    if not _token():
        return []

    pending = _load_pending()
    if not pending:
        return []

    offset = _load_offset()
    updates = _api("getUpdates", offset=offset, timeout=0) or []
    if not updates:
        return []

    ready = []
    new_offset = offset

    for update in updates:
        new_offset = update["update_id"] + 1
        msg = update.get("message", {})
        reply_to = msg.get("reply_to_message", {})
        reply_msg_id = str(reply_to.get("message_id", ""))

        if reply_msg_id not in pending:
            continue

        date_text = (msg.get("text") or "").strip()
        event = pending[reply_msg_id]
        parsed = _parse_user_date(date_text)

        if parsed:
            event["date"] = parsed["date"]
            if parsed.get("start_time"):
                event["start_time"] = parsed["start_time"]
            if parsed.get("end_time"):
                event["end_time"] = parsed["end_time"]
            ready.append(event)
            _remove_pending(reply_msg_id)
            logger.info("Date received for pending event: %s", parsed["date"])
        else:
            _api(
                "sendMessage",
                chat_id=msg.get("chat", {}).get("id"),
                text=(
                    f"Could not parse: <code>{date_text}</code>\n"
                    f"Try: <code>03.12.2026 17:30</code>"
                ),
                parse_mode="HTML",
                reply_to_message_id=msg.get("message_id"),
            )

    if new_offset and new_offset != offset:
        _save_offset(new_offset)

    return ready


def run_bot_loop():
    """Long-polling loop that handles date replies for all users."""
    from pipeline.calendar import create_events
    from pipeline.users import list_active_users, user_dir

    logger.info("Starting Telegram bot loop for date replies")

    while True:
        try:
            all_pending = {}
            for user in list_active_users():
                pf = user_dir(user["id"]) / "pending_events.json"
                pending = _load_pending(pf)
                for msg_id, event in pending.items():
                    event["_user_id"] = user["id"]
                    event["_pending_file"] = str(pf)
                    event["_token_file"] = str(user_dir(user["id"]) / "token.json")
                    all_pending[msg_id] = event

            if not all_pending:
                time.sleep(10)
                continue

            offset = _load_offset()
            updates = _api("getUpdates", offset=offset, timeout=30) or []

            new_offset = offset
            events_by_user = {}

            for update in updates:
                new_offset = update["update_id"] + 1
                msg = update.get("message", {})
                reply_to = msg.get("reply_to_message", {})
                reply_msg_id = str(reply_to.get("message_id", ""))

                if reply_msg_id not in all_pending:
                    continue

                date_text = (msg.get("text") or "").strip()
                event = all_pending[reply_msg_id]
                parsed = _parse_user_date(date_text)

                if parsed:
                    event["date"] = parsed["date"]
                    if parsed.get("start_time"):
                        event["start_time"] = parsed["start_time"]
                    if parsed.get("end_time"):
                        event["end_time"] = parsed["end_time"]

                    uid = event.pop("_user_id")
                    pf = Path(event.pop("_pending_file"))
                    tf = Path(event.pop("_token_file"))

                    if uid not in events_by_user:
                        events_by_user[uid] = {"events": [], "token_file": tf, "chat_id": None}
                    for u in list_active_users():
                        if u["id"] == uid:
                            events_by_user[uid]["chat_id"] = u["telegram_chat_id"]
                            break
                    events_by_user[uid]["events"].append(event)
                    _remove_pending(reply_msg_id, pf)
                    logger.info("Date received for user %s: %s", uid, parsed["date"])
                else:
                    _api(
                        "sendMessage",
                        chat_id=msg.get("chat", {}).get("id"),
                        text=(
                            f"Could not parse: <code>{date_text}</code>\n"
                            f"Try: <code>03.12.2026 17:30</code>"
                        ),
                        parse_mode="HTML",
                        reply_to_message_id=msg.get("message_id"),
                    )

            if new_offset and new_offset != offset:
                _save_offset(new_offset)

            for uid, data in events_by_user.items():
                created = create_events(
                    data["events"], token_file=data["token_file"]
                )
                for ev, cal_ev in zip(data["events"], created):
                    notify_created(ev, cal_ev.get("htmlLink"), chat_id=data["chat_id"])

        except KeyboardInterrupt:
            logger.info("Bot loop stopped")
            break
        except Exception:
            logger.exception("Bot loop error")
            time.sleep(30)
