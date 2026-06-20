# Call-for-Papers Deadline Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect Call-for-Papers posts in scraped LinkedIn saved posts, extract the submission deadline, create an all-day Google Calendar event with a 14-day reminder, and send a Telegram message 14 days before the deadline.

**Architecture:** A new stdlib-only module `pipeline/cfp.py` holds the keyword gate and per-user reminder state. The gate runs inside `classify_post`; a CfP routes to a deadline-extraction prompt and is tagged `classification="call_for_papers"`, which flows unchanged through the existing event pipeline. `calendar._build_event_body` gives CfP events an all-day shape + 14-day reminder. `__main__` records created CfP deadlines and, on each daily run, fires Telegram reminders for deadlines now within 14 days.

**Tech Stack:** Python 3.11, pytest 9.0.3 (in `.venv`), Anthropic SDK, Google Calendar API, Telegram Bot API.

## Global Constraints

- `pipeline/cfp.py` MUST import only the standard library (`json`, `re`, `datetime`, `pathlib`, `logging`) so its unit tests need no network/API deps.
- Run tests with: `.venv/Scripts/python.exe -m pytest tests/<file> -v` (Windows; repo `pytest.ini` sets `testpaths = tests`).
- New tests live in `tests/` (e.g. `tests/test_cfp.py`, `tests/test_calendar_cfp.py`).
- Reminder interval is hard-coded at 14 days = `20160` minutes = `within_days=14`.
- Classification value for CfP is exactly the string `"call_for_papers"` everywhere.
- Per-user reminder state file is `<user_dir>/cfp_reminders.json`; `<user_dir>` is `token_file.parent` at every creation site.
- Deploy to the VM via base64+ssh (no `git pull`), then `sudo systemctl restart linkedin-bot` — see Task 7.

---

### Task 1: CfP keyword gate (`pipeline/cfp.py`)

**Files:**
- Create: `pipeline/cfp.py`
- Test: `tests/test_cfp.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `is_call_for_papers(text: str) -> bool`
  - module constants `CFP_SUBSTRINGS: list[str]`, `CFP_BOUNDARY_WORDS: list[str]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cfp.py
import pytest
from pipeline.cfp import is_call_for_papers


@pytest.mark.parametrize("text", [
    "Call for Papers: submit by 30 June",
    "CALL FOR PAPERS — deadline soon",
    "We publish a call for paper for the workshop",
    "Achtung: call for papres bis 1. Juli",          # typo variant
    "Reminder: call for papre still open",            # typo variant
    "Join our #CfP for the conference",               # boundary word
    "See the call4papers page",                       # boundary word
])
def test_detects_cfp(text):
    assert is_call_for_papers(text) is True


@pytest.mark.parametrize("text", [
    "We are hiring a Senior Policy Analyst",
    "Save the date for our annual gala",
    "",
    "The scfposter was great",          # 'cfp' inside another word -> no match
    "https://example.com/cfpage",       # 'cfp' inside a URL token -> no match
])
def test_ignores_non_cfp(text):
    assert is_call_for_papers(text) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cfp.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pipeline.cfp'`

- [ ] **Step 3: Write minimal implementation**

```python
# pipeline/cfp.py
"""Call-for-Papers detection and deadline-reminder state.

Standard-library only — keep it import-light so unit tests need no
network or API dependencies.
"""
from __future__ import annotations

import re

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cfp.py -v`
Expected: PASS (all parametrized cases)

- [ ] **Step 5: Commit**

```bash
git add pipeline/cfp.py tests/test_cfp.py
git commit -m "feat(cfp): add Call-for-Papers keyword gate"
```

---

### Task 2: CfP reminder state — record & due (`pipeline/cfp.py`)

**Files:**
- Modify: `pipeline/cfp.py`
- Test: `tests/test_cfp.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `REMINDER_FILE_NAME = "cfp_reminders.json"`
  - `record_deadlines(state_dir: Path, events: list[dict], calendar_urls: list) -> int`
    — appends one record per event whose `classification == "call_for_papers"` and
    truthy `date`; dedupes by `post_id`; returns count newly recorded. Each record:
    `{"post_id", "title", "deadline", "calendar_url", "reminded": False}`.
  - `due_reminders(state_dir: Path, today: date, within_days: int = 14) -> list[dict]`
    — prunes records with `deadline < today`; returns records with
    `0 <= (deadline - today).days <= within_days` and `reminded is False`, each
    augmented with `"days_left": int`; marks those `reminded = True`; persists.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cfp.py  (append)
import json
from datetime import date
from pathlib import Path

from pipeline.cfp import record_deadlines, due_reminders, REMINDER_FILE_NAME


def _ev(post_id, date_str, cls="call_for_papers", title="T"):
    return {"post_id": post_id, "classification": cls, "title": title, "date": date_str}


def test_record_only_dated_cfps(tmp_path):
    events = [
        _ev("p1", "2026-08-01"),
        _ev("p2", None),                       # no date -> skip
        _ev("p3", "2026-08-02", cls="invitation"),  # not cfp -> skip
    ]
    n = record_deadlines(tmp_path, events, ["http://cal/p1", None, "http://cal/p3"])
    assert n == 1
    data = json.loads((tmp_path / REMINDER_FILE_NAME).read_text(encoding="utf-8"))
    assert [r["post_id"] for r in data] == ["p1"]
    assert data[0]["reminded"] is False
    assert data[0]["calendar_url"] == "http://cal/p1"


def test_record_dedupes_by_post_id(tmp_path):
    record_deadlines(tmp_path, [_ev("p1", "2026-08-01")], ["u1"])
    n = record_deadlines(tmp_path, [_ev("p1", "2026-08-01")], ["u1"])
    assert n == 0
    data = json.loads((tmp_path / REMINDER_FILE_NAME).read_text(encoding="utf-8"))
    assert len(data) == 1


def test_due_within_window_marks_reminded(tmp_path):
    record_deadlines(tmp_path, [_ev("p1", "2026-08-10")], ["u1"])
    due = due_reminders(tmp_path, date(2026, 8, 1))   # 9 days out
    assert [r["post_id"] for r in due] == ["p1"]
    assert due[0]["days_left"] == 9
    # second call: already reminded -> nothing
    assert due_reminders(tmp_path, date(2026, 8, 1)) == []


def test_due_ignores_far_future(tmp_path):
    record_deadlines(tmp_path, [_ev("p1", "2026-09-30")], ["u1"])
    assert due_reminders(tmp_path, date(2026, 8, 1)) == []   # 60 days out


def test_due_prunes_past(tmp_path):
    record_deadlines(tmp_path, [_ev("p1", "2026-07-01")], ["u1"])
    assert due_reminders(tmp_path, date(2026, 8, 1)) == []   # past -> pruned
    data = json.loads((tmp_path / REMINDER_FILE_NAME).read_text(encoding="utf-8"))
    assert data == []


def test_due_missing_file_is_empty(tmp_path):
    assert due_reminders(tmp_path, date(2026, 8, 1)) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cfp.py -v`
Expected: FAIL with `ImportError: cannot import name 'record_deadlines'`

- [ ] **Step 3: Write minimal implementation**

```python
# pipeline/cfp.py  (append; add imports at top)
import json
import logging
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cfp.py -v`
Expected: PASS (all cases)

- [ ] **Step 5: Commit**

```bash
git add pipeline/cfp.py tests/test_cfp.py
git commit -m "feat(cfp): add deadline reminder state (record + due)"
```

---

### Task 3: Calendar event shape for CfP (`pipeline/calendar.py`)

**Files:**
- Modify: `pipeline/calendar.py` (`_build_event_body`, around lines 113-150)
- Test: `tests/test_calendar_cfp.py`

**Interfaces:**
- Consumes: `is_call_for_papers` not needed here — branch on `classification == "call_for_papers"`.
- Produces: `_build_event_body` returns, for CfP, an all-day event with summary
  prefixed `"CfP Deadline: "` and `reminders.overrides` at `20160` minutes.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_calendar_cfp.py
from pipeline.calendar import _build_event_body


def test_cfp_event_is_allday_with_14day_reminder():
    body = _build_event_body({
        "classification": "call_for_papers",
        "title": "ICA 2027 Annual Conference",
        "date": "2026-11-15",
        "start_time": "09:00",          # ignored for CfP (all-day)
        "description": "Submit abstracts",
        "post_id": "abc123",
    })
    assert body["summary"] == "CfP Deadline: ICA 2027 Annual Conference"
    assert body["start"] == {"date": "2026-11-15"}
    assert body["end"] == {"date": "2026-11-15"}
    assert body["reminders"]["useDefault"] is False
    minutes = sorted(o["minutes"] for o in body["reminders"]["overrides"])
    assert minutes == [20160, 20160]
    methods = sorted(o["method"] for o in body["reminders"]["overrides"])
    assert methods == ["email", "popup"]


def test_non_cfp_event_unchanged():
    body = _build_event_body({
        "classification": "invitation",
        "title": "Workshop",
        "date": "2026-07-07",
        "start_time": "09:00",
        "post_id": "x",
    })
    assert body["summary"] == "Workshop"
    assert "reminders" not in body
    assert body["start"] == {"dateTime": "2026-07-07T09:00:00", "timeZone": "Europe/Berlin"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_calendar_cfp.py -v`
Expected: FAIL — `test_cfp_event_is_allday_with_14day_reminder` (summary has no prefix; no `reminders` key)

- [ ] **Step 3: Write minimal implementation**

In `pipeline/calendar.py`, inside `_build_event_body`, after the existing variable
extraction (`post_id = classification.get("post_id", "")`) and before
`event: dict[str, Any] = {...}`, add the CfP flag and adjust. Replace the body
construction block (current lines ~121-150) with:

```python
    post_id = classification.get("post_id", "")
    is_cfp = classification.get("classification") == "call_for_papers"

    if description and post_id:
        description += f"\n\n[pipeline post_id: {post_id}]"

    if is_cfp:
        title = f"CfP Deadline: {title}"

    event: dict[str, Any] = {
        "summary": title,
        "description": description,
    }

    if location:
        event["location"] = location

    if is_cfp and date_str:
        # A submission deadline is a day, not a meeting: all-day + 14-day reminder.
        event["start"] = {"date": date_str}
        event["end"] = {"date": date_str}
        event["reminders"] = {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 20160},
                {"method": "email", "minutes": 20160},
            ],
        }
    elif date_str and start_time:
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_calendar_cfp.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add pipeline/calendar.py tests/test_calendar_cfp.py
git commit -m "feat(cfp): all-day calendar event with 14-day reminder for CfP"
```

---

### Task 4: Classifier gate + deadline prompt (`pipeline/classifier.py`)

**Files:**
- Modify: `pipeline/classifier.py`
- Test: `tests/test_classifier_cfp.py`

**Interfaces:**
- Consumes: `pipeline.cfp.is_call_for_papers`.
- Produces:
  - `CFP_SYSTEM_PROMPT: str`
  - `classify_post` returns `classification == "call_for_papers"` with
    `date` = submission deadline for CfP posts (gate-driven, not model-driven).
  - `_classify_post_local` returns `call_for_papers` for gated posts in dry-run.
  - `"call_for_papers"` added to the accepted-classification set.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_classifier_cfp.py
import pipeline.classifier as clf


def test_local_classifier_flags_cfp():
    post = {"author": "ACME", "text": "Call for Papers — submit by 30. November 2026"}
    result = clf._classify_post_local(post)
    assert result["classification"] == "call_for_papers"
    assert result["date"] == "2026-11-30"


def test_classify_post_uses_cfp_prompt_and_forces_label(monkeypatch):
    captured = {}

    class _Block:
        type = "text"
        text = '{"classification": "save_the_date", "title": "X Conf", "date": "2027-01-15", "start_time": null, "end_time": null, "location": null, "description": "deadline"}'

    class _Resp:
        content = [_Block()]

    class _Messages:
        def create(self, **kwargs):
            captured["system"] = kwargs["system"]
            return _Resp()

    class _Client:
        messages = _Messages()

    monkeypatch.setattr(clf, "_get_client", lambda: _Client())

    post = {"author": "ACME", "text": "Our CfP is open, deadline 15 Jan 2027"}
    result = clf.classify_post(post)

    # Gate forces the CfP prompt and overrides the label even if the model says otherwise.
    assert captured["system"] == clf.CFP_SYSTEM_PROMPT
    assert result["classification"] == "call_for_papers"
    assert result["date"] == "2027-01-15"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_classifier_cfp.py -v`
Expected: FAIL — `_classify_post_local` returns `none`; `CFP_SYSTEM_PROMPT` missing.

- [ ] **Step 3: Write minimal implementation**

3a. Add the import and prompt near the top of `pipeline/classifier.py` (after `import anthropic`):

```python
from pipeline import cfp

CFP_SYSTEM_PROMPT = """\
You are extracting the submission deadline from a LinkedIn "Call for Papers" post.

Respond with a single JSON object — no markdown fences, no commentary.

Schema:

{
  "classification": "call_for_papers",
  "title": "string or null",
  "date": "ISO 8601 date or null",
  "start_time": null,
  "end_time": null,
  "location": "string or null",
  "description": "one-line summary or null"
}

Rules:
- "date" MUST be the SUBMISSION DEADLINE — the last date to submit papers or
  abstracts. This is NOT the conference/event date.
- If a submission date RANGE is given, use the END date.
- If no submission deadline can be found, set "date" to null.
- Dates are ISO 8601 (YYYY-MM-DD). If only a month is given, use its last day.
- "title" is the conference/workshop/journal name. Keep "description" to one sentence.
"""
```

3b. In `classify_post`, branch on the gate before calling the API. Replace the
block that sets `user_message`/`model` and makes the request so the system prompt
and forced label depend on the gate:

```python
    author = post.get("author", "")
    text = post.get("text", "")

    if not text:
        logger.debug("Skipping post with empty text (post_id=%s)", post.get("post_id"))
        return dict(NONE_RESULT)

    is_cfp = cfp.is_call_for_papers(text)
    system_prompt = CFP_SYSTEM_PROMPT if is_cfp else SYSTEM_PROMPT
    user_message = f"Author: {author}\n\nPost:\n{text}"
    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)

    try:
        client = _get_client()
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
    except RuntimeError as exc:
        logger.warning("%s — falling back to keyword classifier", exc)
        return _classify_post_local(post)
    except anthropic.AuthenticationError:
        logger.error("Invalid ANTHROPIC_API_KEY — check your key")
        return dict(NONE_RESULT)
    except anthropic.APIError as exc:
        logger.error("Anthropic API error: %s", exc)
        return dict(NONE_RESULT)

    response_text = next(
        (block.text for block in response.content if block.type == "text"), ""
    )

    result = _parse_json(response_text)
    if result is None:
        return dict(NONE_RESULT)

    if is_cfp:
        classification = "call_for_papers"     # gate decides; model can't override
    else:
        classification = result.get("classification", "none")
        if classification not in ("invitation", "save_the_date", "none"):
            logger.warning("Unknown classification %r — defaulting to none", classification)
            classification = "none"

    return {
        "classification": classification,
        "title": result.get("title"),
        "date": result.get("date"),
        "start_time": result.get("start_time"),
        "end_time": result.get("end_time"),
        "location": result.get("location"),
        "description": result.get("description"),
    }
```

3c. In `_classify_post_local`, add a CfP branch at the top (before the
INVITE/SAVE_DATE keyword scan). Insert right after `author = post.get("author") or ""`:

```python
    if cfp.is_call_for_papers(text):
        date_str = None
        m = DATE_PATTERN.search(post.get("text") or "")
        if m:
            day = m.group(1).zfill(2)
            month = MONTH_MAP[m.group(2).lower()]
            year = m.group(3) or str(__import__("datetime").date.today().year)
            date_str = f"{year}-{month}-{day}"
        return {
            "classification": "call_for_papers",
            "title": f"Call for Papers from {author}" if author else "Call for Papers",
            "date": date_str,
            "start_time": None,
            "end_time": None,
            "location": None,
            "description": (post.get("text") or "")[:100],
        }
```

(Note: `text` in `_classify_post_local` is already lowercased; `is_call_for_papers`
lowercases internally so passing either is fine.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_classifier_cfp.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Run the full classifier-related suite to check no regressions**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cfp.py tests/test_calendar_cfp.py tests/test_classifier_cfp.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add pipeline/classifier.py tests/test_classifier_cfp.py
git commit -m "feat(cfp): keyword-gated CfP classification + deadline prompt"
```

---

### Task 5: Telegram deadline-reminder message (`pipeline/telegram.py`)

**Files:**
- Modify: `pipeline/telegram.py`
- Test: `tests/test_telegram_cfp.py`

**Interfaces:**
- Consumes: existing `_api`, `_chat_id`, `_token`.
- Produces: `notify_deadline_reminder(record: dict, chat_id: str | None = None) -> None`
  where `record` has keys `title`, `deadline`, `days_left`, optional `calendar_url`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_telegram_cfp.py
import pipeline.telegram as tg


def test_notify_deadline_reminder_sends_expected(monkeypatch):
    calls = []
    monkeypatch.setattr(tg, "_token", lambda: "TESTTOKEN")
    monkeypatch.setattr(tg, "_api", lambda method, **params: calls.append((method, params)) or {"message_id": 1})

    tg.notify_deadline_reminder(
        {"title": "ICA 2027", "deadline": "2026-11-15", "days_left": 14,
         "calendar_url": "http://cal/x"},
        chat_id="999",
    )

    assert len(calls) == 1
    method, params = calls[0]
    assert method == "sendMessage"
    assert params["chat_id"] == "999"
    assert "14" in params["text"]
    assert "ICA 2027" in params["text"]
    assert "2026-11-15" in params["text"]
    assert "http://cal/x" in params["text"]


def test_notify_deadline_reminder_noop_without_chat(monkeypatch):
    calls = []
    monkeypatch.setattr(tg, "_token", lambda: "TESTTOKEN")
    monkeypatch.setattr(tg, "_chat_id", lambda: None)
    monkeypatch.setattr(tg, "_api", lambda *a, **k: calls.append(1))
    tg.notify_deadline_reminder({"title": "X", "deadline": "2026-01-01", "days_left": 3})
    assert calls == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_telegram_cfp.py -v`
Expected: FAIL — `AttributeError: module 'pipeline.telegram' has no attribute 'notify_deadline_reminder'`

- [ ] **Step 3: Write minimal implementation**

Add to `pipeline/telegram.py` after `notify_created`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_telegram_cfp.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add pipeline/telegram.py tests/test_telegram_cfp.py
git commit -m "feat(cfp): add Telegram CfP deadline reminder message"
```

---

### Task 6: Wire recording + daily reminder pass (`pipeline/__main__.py`, `pipeline/telegram.py`)

**Files:**
- Modify: `pipeline/__main__.py` (`_run_pipeline` creation block ~104-116; `main()` after the per-user loop ~230)
- Modify: `pipeline/telegram.py` (`run_bot_loop` creation block ~311-316)
- Test: `tests/test_pipeline_wiring_cfp.py`

**Interfaces:**
- Consumes: `cfp.record_deadlines`, `cfp.due_reminders`, `telegram.notify_deadline_reminder`.
- Produces: CfP events recorded at both creation sites; a daily pass that fires reminders.

- [ ] **Step 1: Write the failing test** (covers the daily reminder pass logic, the highest-value new wiring)

```python
# tests/test_pipeline_wiring_cfp.py
from datetime import date

import pipeline.cfp as cfp
import pipeline.__main__ as mainmod


def test_run_due_reminders_notifies(tmp_path, monkeypatch):
    # One user, one CfP deadline 10 days out, already recorded.
    user_dir = tmp_path / "user_x"
    user_dir.mkdir()
    cfp.record_deadlines(
        user_dir,
        [{"post_id": "p1", "classification": "call_for_papers",
          "title": "ICA", "date": "2026-08-11"}],
        ["http://cal/p1"],
    )

    monkeypatch.setattr(mainmod, "list_active_users",
                        lambda: [{"id": "x", "telegram_chat_id": "555"}])
    monkeypatch.setattr(mainmod, "user_dir", lambda uid: tmp_path / f"user_{uid}")

    sent = []
    monkeypatch.setattr(mainmod, "notify_deadline_reminder",
                        lambda rec, chat_id=None: sent.append((rec["title"], chat_id, rec["days_left"])))

    mainmod._run_due_reminders(today=date(2026, 8, 1))

    assert sent == [("ICA", "555", 10)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_pipeline_wiring_cfp.py -v`
Expected: FAIL — `AttributeError: module 'pipeline.__main__' has no attribute '_run_due_reminders'` (and `notify_deadline_reminder` not imported there)

- [ ] **Step 3: Write minimal implementation**

3a. In `pipeline/__main__.py`, add imports near the existing pipeline imports
(after `from pipeline.users import list_active_users, user_dir`):

```python
from datetime import date

import pipeline.cfp as cfp
from pipeline.telegram import notify_created, notify_missing_date, notify_deadline_reminder
```

(`notify_created`/`notify_missing_date` are already imported via the existing
`from pipeline.telegram import ...` line — merge `notify_deadline_reminder` into it
rather than duplicating.)

3b. In `_run_pipeline`, replace the `with_date` creation/notify block (current
lines ~104-110) so CfP events are recorded after creation:

```python
    created_count = 0
    if with_date:
        logger.info("Creating %d calendar event(s) with dates", len(with_date))
        created = create_events(with_date, token_file=token_file)
        created_count = len(created)
        cfp_events, cfp_urls = [], []
        for ev, cal_ev in zip(with_date, created):
            url = cal_ev.get("htmlLink")
            notify_created(ev, url, chat_id=chat_id)
            if ev.get("classification") == "call_for_papers":
                cfp_events.append(ev)
                cfp_urls.append(url)
        if cfp_events and token_file is not None:
            cfp.record_deadlines(Path(token_file).parent, cfp_events, cfp_urls)
```

(`Path` is already imported in `__main__.py`.)

3c. In `pipeline/__main__.py`, add the daily-pass function (module level, e.g.
just above `def main()`):

```python
def _run_due_reminders(today=None):
    """Send Telegram reminders for CfP deadlines now within the reminder window."""
    today = today or date.today()
    for user in list_active_users():
        ud = user_dir(user["id"])
        for rec in cfp.due_reminders(ud, today):
            notify_deadline_reminder(rec, chat_id=user["telegram_chat_id"])
```

3d. In `main()`, call it once per run after the `for user in users:` loop and
before the `if not args.loop: break` (i.e. right after
`logger.info("Run #%d complete", run_count)`):

```python
        try:
            _run_due_reminders()
        except Exception:
            logger.exception("CfP reminder pass failed")
```

3e. In `pipeline/telegram.py` `run_bot_loop`, record CfP deadlines after the
reply-driven creation (current lines ~311-316):

```python
            for uid, data in events_by_user.items():
                created = create_events(
                    data["events"], token_file=data["token_file"]
                )
                cfp_events, cfp_urls = [], []
                for ev, cal_ev in zip(data["events"], created):
                    url = cal_ev.get("htmlLink")
                    notify_created(ev, url, chat_id=data["chat_id"])
                    if ev.get("classification") == "call_for_papers":
                        cfp_events.append(ev)
                        cfp_urls.append(url)
                if cfp_events:
                    from pipeline import cfp
                    cfp.record_deadlines(data["token_file"].parent, cfp_events, cfp_urls)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_pipeline_wiring_cfp.py -v`
Expected: PASS

- [ ] **Step 5: Run the full new suite + a smoke import**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cfp.py tests/test_calendar_cfp.py tests/test_classifier_cfp.py tests/test_telegram_cfp.py tests/test_pipeline_wiring_cfp.py -v`
Expected: PASS
Run: `.venv/Scripts/python.exe -c "import pipeline.__main__, pipeline.telegram, pipeline.classifier, pipeline.calendar, pipeline.cfp; print('imports OK')"`
Expected: `imports OK`

- [ ] **Step 6: Commit**

```bash
git add pipeline/__main__.py pipeline/telegram.py tests/test_pipeline_wiring_cfp.py
git commit -m "feat(cfp): record deadlines on creation + daily reminder pass"
```

---

### Task 7: Dry-run verification, deploy, and live smoke test

**Files:** none (operational)

- [ ] **Step 1: Local dry-run against a synthetic CfP post**

Create `tmp_cfp.json` (scratchpad) with one CfP post, then:

```bash
.venv/Scripts/python.exe -m pipeline --input tmp_cfp.json --dry-run
```

Example `tmp_cfp.json`:
```json
[{"post_id": "cfp-test-1", "author": "Conf Org",
  "text": "Call for Papers: ICA 2027. Submission deadline 30. November 2026. Vienna."}]
```
Expected log: `... → call_for_papers (...)` and a dry-run line showing it would create an event. (Dry-run uses the local classifier; no calendar/API.)

- [ ] **Step 2: Push to GitHub**

```bash
git push
```

- [ ] **Step 3: Deploy changed files to the VM (base64+ssh)**

For each changed file (`pipeline/cfp.py`, `pipeline/classifier.py`,
`pipeline/calendar.py`, `pipeline/telegram.py`, `pipeline/__main__.py`):

```bash
B64=$(base64 -w0 pipeline/<file>)
gcloud compute ssh job-checker-vm --zone=us-central1-a \
  --command="echo '$B64' | base64 -d > /home/kerbel/linkedin-pipeline/pipeline/<file>"
```

Then syntax-check and restart the bot:

```bash
gcloud compute ssh job-checker-vm --zone=us-central1-a --command="cd /home/kerbel/linkedin-pipeline && python3 -c 'import pipeline.__main__, pipeline.cfp; print(\"VM imports OK\")' && sudo systemctl restart linkedin-bot && systemctl is-active linkedin-bot"
```
Expected: `VM imports OK` then `active`.

- [ ] **Step 4: Live smoke test (real run for one user)**

Trigger a real run (the existing `run_xenia.py` helper, or `python3 -m pipeline`)
and confirm in the log: any CfP post is labelled `call_for_papers`, a calendar
event `CfP Deadline: …` is created, and `cfp_reminders.json` appears under the
user dir. If a recorded deadline is already within 14 days, confirm a
`notify_deadline_reminder` Telegram message arrives.

```bash
gcloud compute ssh job-checker-vm --zone=us-central1-a --command="cat /home/kerbel/.linkedin-mcp/user_xenia/cfp_reminders.json 2>/dev/null || echo 'no CfP recorded this run'"
```

- [ ] **Step 5: Final commit (if any fixups were needed during verification)**

```bash
git add -A && git commit -m "fix(cfp): verification fixups" && git push
```

---

## Self-Review

**Spec coverage:**
- Detection keyword gate → Task 1 ✔
- Deadline extraction via LLM → Task 4 ✔
- CfP calendar event all-day + 14-day Google reminder → Task 3 ✔
- Telegram 14-day reminder (state + daily pass + message) → Tasks 2, 5, 6 ✔
- No-deadline fallback via existing missing-date flow → Task 4 (date stays null → `_run_pipeline` routes to `notify_missing_date`; classification rides along) + Task 6 (reply-path recording in `run_bot_loop`) ✔
- Applies to all active users → Task 6 daily pass iterates `list_active_users` ✔
- Files touched match spec table → cfp.py, classifier.py, calendar.py, telegram.py, __main__.py ✔

**Placeholder scan:** No TBD/TODO/"add error handling"; every code step shows complete code. ✔

**Type consistency:** `is_call_for_papers(text)->bool`, `record_deadlines(state_dir, events, calendar_urls)->int`, `due_reminders(state_dir, today, within_days=14)->list[dict]` (adds `days_left`), `notify_deadline_reminder(record, chat_id=None)`, `_run_due_reminders(today=None)`, classification string `"call_for_papers"`, reminder file `cfp_reminders.json`, reminder minutes `20160` — all consistent across tasks. ✔

**Note on no-deadline fallback test:** the routing itself (`without_date` → `notify_missing_date`) is existing untouched code; its CfP behavior is exercised in the live smoke test (Task 7) rather than a unit test, since it spans the bot reply loop.
