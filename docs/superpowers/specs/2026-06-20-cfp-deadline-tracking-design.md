# Call-for-Papers Deadline Tracking — Design

**Date:** 2026-06-20
**Status:** Approved (pending spec review)

## Goal

Detect Call-for-Papers (CfP) posts among scraped LinkedIn saved posts, extract
the submission **deadline** (end date), create a Google Calendar event on that
date, and notify the user **two weeks before** the deadline via *both* a native
Google Calendar reminder *and* a Telegram message.

This runs inside the shared multi-user pipeline, so it applies to every active
user (currently `kerbel` and `xenia`).

## Background

The existing pipeline (`pipeline/`) scrapes saved posts, classifies each via the
Claude API as `invitation` / `save_the_date` / `none`, creates Google Calendar
events for the non-`none` ones, and sends a Telegram notification on creation.
Events with no extractable date fall back to an interactive Telegram
force-reply flow (`notify_missing_date` → `run_bot_loop` parses the reply →
creates the event).

The classification dict for each post is the single object that flows through
the whole pipeline — including being persisted to `pending_events.json` and read
back in the bot reply loop. Any field added to it propagates end-to-end without
extra plumbing. The CfP feature exploits this: a `call_for_papers` marker on the
classification dict is enough to drive special calendar/reminder behavior in
every downstream path.

## Decisions (from brainstorming)

1. **Reminder type:** Both — a native Google Calendar reminder 14 days before
   AND a Telegram message 14 days before.
2. **Detection:** Keyword gate decides CfP; the LLM extracts the deadline.
3. **No deadline found:** Route through the existing Telegram missing-date prompt
   so the user can supply the deadline by reply.

## Detailed Design

### 1. Detection — keyword gate (`pipeline/cfp.py`)

New module `pipeline/cfp.py`.

`is_call_for_papers(text) -> bool`: case-insensitive detection.
- Substring match on: `"call for papers"`, `"call for paper"`,
  `"call for papres"`, `"call for papre"`.
- Word-boundary regex match on: `"cfp"`, `"call4papers"` (boundary-guarded to
  avoid matching inside URLs / longer tokens).

The gate runs in `classify_post` **before** the normal classification. If it
matches, the post is treated as a CfP regardless of any invitation/save-the-date
cues (the deadline is the relevant signal).

### 2. Deadline extraction — LLM (`pipeline/classifier.py`)

When the gate matches, `classify_post` calls the Claude API with a CfP-specific
system prompt (`CFP_SYSTEM_PROMPT`) that returns the same JSON schema as the
existing classifier, but:
- `classification` is `"call_for_papers"`.
- `date` is the **submission deadline** — explicitly instructed: "Extract the
  last date to submit (the submission deadline). If a date range is given, use
  the END date. This is NOT the conference/event date." ISO 8601 (`YYYY-MM-DD`).
- `title`, `location`, `description` extracted as usual.

`"call_for_papers"` is added to the set of accepted classification values in
`classify_post` (currently `invitation` / `save_the_date` / `none`). Because it
is `!= "none"`, it flows through `_run_pipeline`'s event filtering, the
with-date / without-date split, and `create_events` unchanged.

The dry-run keyword classifier (`_classify_post_local`) also gains a CfP branch
for parity (gate → `call_for_papers`, regex date as best-effort), so `--dry-run`
stays usable without the API.

### 3. Calendar event + native reminder (`pipeline/calendar.py`)

In `_build_event_body`, when `classification.get("classification") ==
"call_for_papers"`:
- Summary prefixed: `CfP Deadline: <title>`.
- **All-day** event on the deadline `date` (start `date` == end `date`), even if
  a time were present — a deadline is a day, not a meeting.
- `event["reminders"] = {"useDefault": False, "overrides": [
    {"method": "popup", "minutes": 20160},
    {"method": "email", "minutes": 20160}]}` (20160 min = 14 days).
- Description keeps the post snippet + `[pipeline post_id: ...]` as today.

Non-CfP events are unchanged.

### 4. Telegram reminder 14 days before — per-user state + daily pass

**State file:** `~/.linkedin-mcp/user_<id>/cfp_reminders.json` — a JSON list of
records: `{"post_id", "title", "deadline" (ISO date), "calendar_url",
"reminded" (bool)}`.

Functions in `pipeline/cfp.py`:
- `record_deadlines(state_dir, events, calendar_urls)` — for each created event
  whose `classification == "call_for_papers"` and has a `date`, append a record
  (deduped by `post_id`; skip if already present). `state_dir` is the user dir;
  `calendar_urls` are the `htmlLink`s returned by `create_events`.
- `due_reminders(state_dir, today, within_days=14)` — return records where
  `0 <= (deadline - today).days <= within_days` and `reminded == False`; mark
  them `reminded = True` and persist. Also prune records whose deadline is in the
  past (`deadline < today`).

**Recording** happens at both creation sites:
- `__main__._run_pipeline` (daily run): after `create_events`, call
  `record_deadlines` for the user.
- `telegram.run_bot_loop` (reply path): after `create_events`, call
  `record_deadlines` for the user — so deadlines supplied via reply are tracked
  too.

**Firing** happens in `__main__.main()`: after the per-user processing loop, add
a pass over active users calling `due_reminders` and, for each due record,
`telegram.notify_deadline_reminder(record, days_left, chat_id)`. Because the
cron runs `python -m pipeline` daily, this is checked once per day.

`notify_deadline_reminder(record, days_left, chat_id)` in `telegram.py` sends:
`⏰ <b>CfP deadline in {days_left} day(s)</b>\n<title>\n📅 {deadline}` plus the
calendar link if present.

### 5. No-deadline fallback

If the gate matches but the LLM returns `date: null`, `_run_pipeline` already
routes date-less events to `notify_missing_date` (force-reply). The
`call_for_papers` classification rides along in the pending dict. When the user
replies with a date:
- `run_bot_loop` sets `date` and calls `create_events` → `_build_event_body`
  sees `call_for_papers` → adds the 14-day override (section 3).
- `run_bot_loop` then calls `record_deadlines` (section 4) → the Telegram 14-day
  reminder is tracked.

No change to the reply parser is needed.

## Files Touched

| File | Change |
|------|--------|
| `pipeline/cfp.py` | **new** — keyword gate, reminder state (record / due / prune) |
| `pipeline/classifier.py` | CfP gate + `CFP_SYSTEM_PROMPT`; accept `call_for_papers`; dry-run parity |
| `pipeline/calendar.py` | CfP event shape (all-day, title prefix) + 14-day reminder override |
| `pipeline/telegram.py` | `notify_deadline_reminder`; `record_deadlines` call in reply path |
| `pipeline/__main__.py` | `record_deadlines` after creation; daily due-reminder pass |

## Data Flow

```
saved post
  └─ classify_post
       ├─ is_call_for_papers(text)? ──no──> existing invitation/save_the_date/none
       └─ yes ─> CFP_SYSTEM_PROMPT ─> {classification: call_for_papers, date: deadline, ...}
                   ├─ date present ─> create_events ─> CfP event (all-day + 14d override)
                   │                   └─ record_deadlines() ─> cfp_reminders.json
                   └─ date null ────> notify_missing_date ─> (reply) ─> create_events + record_deadlines

daily run (cron):
  for each user: due_reminders(today) ─> notify_deadline_reminder() for deadlines within 14 days
```

## Edge Cases & Notes

- **Already within 14 days at discovery:** `due_reminders` fires on the next
  daily run (effectively immediately), in addition to the creation notice. Minor
  redundancy, accepted (YAGNI — no dedup against the creation notice).
- **`cfp` false positives:** word-boundary regex guards against matching inside
  URLs/hashtags; the `"call for paper…"` variants are plain substrings.
- **Timezone:** `due_reminders` uses `date.today()` (VM is UTC). The 14-day
  window is coarse enough that UTC vs Europe/Berlin is immaterial; calendar
  events keep `Europe/Berlin` where applicable, but CfP events are all-day so
  timezone does not apply.
- **Dedup:** `record_deadlines` keys on `post_id`; the pipeline already dedups
  posts via `processed_posts.json`, so a CfP is recorded once.
- **Past deadlines:** pruned by `due_reminders` so the state file stays small.

## Out of Scope

- Editing/cancelling reminders after creation.
- Reminder intervals other than 14 days (hard-coded, matches the request).
- Detecting CfPs that never use a CfP phrase (the gate is intentionally literal).
