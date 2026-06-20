import json
from datetime import date
from pathlib import Path

import pytest
from pipeline.cfp import is_call_for_papers, record_deadlines, due_reminders, REMINDER_FILE_NAME


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
