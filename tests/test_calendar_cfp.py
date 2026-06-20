import pytest
from unittest.mock import MagicMock

import pipeline.calendar as cal_module
from pipeline.calendar import _build_event_body, create_events


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


def test_create_events_alignment_on_middle_failure(monkeypatch):
    """
    When the MIDDLE event's insert raises, create_events must still return
    a list of length 3 with None in position 1 (not drop it).
    RED: current code drops the failed item → length 2.
    """
    classifications = [
        {"classification": "invitation", "title": "Event A", "date": "2026-07-01", "post_id": "a"},
        {"classification": "call_for_papers", "title": "Event B", "date": "2026-08-01", "post_id": "b"},
        {"classification": "invitation", "title": "Event C", "date": "2026-09-01", "post_id": "c"},
    ]

    good_resource_a = {"htmlLink": "https://cal.google.com/a", "summary": "Event A"}
    good_resource_c = {"htmlLink": "https://cal.google.com/c", "summary": "Event C"}

    call_count = [0]

    def fake_execute():
        call_count[0] += 1
        if call_count[0] == 2:
            raise RuntimeError("API error for middle event")
        if call_count[0] == 1:
            return good_resource_a
        return good_resource_c

    fake_insert = MagicMock()
    fake_insert.return_value.execute = fake_execute
    fake_events = MagicMock()
    fake_events.return_value.insert = fake_insert
    fake_service = MagicMock()
    fake_service.events = fake_events

    monkeypatch.setattr(cal_module, "_get_service", lambda *args, **kwargs: fake_service)

    result = create_events(classifications)

    # Must be length 3, preserving alignment
    assert len(result) == 3, f"Expected 3 items (with None for failure), got {len(result)}: {result}"
    assert result[0] == good_resource_a
    assert result[1] is None
    assert result[2] == good_resource_c


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
