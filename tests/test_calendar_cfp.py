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
