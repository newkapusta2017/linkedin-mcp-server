import json
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


def test_notify_missing_date_persists_classification(tmp_path, monkeypatch):
    """
    notify_missing_date must persist the full event dict (including
    classification == "call_for_papers") into pending_events.json so the
    bot reply path can reconstruct a CfP calendar event.
    """
    pending_file = tmp_path / "pending_events.json"

    monkeypatch.setattr(tg, "_token", lambda: "TESTTOKEN")
    monkeypatch.setattr(tg, "_reply_chat_id", lambda: "12345")
    monkeypatch.setattr(
        tg,
        "_api",
        lambda method, **params: {"message_id": 42},
    )

    event = {
        "post_id": "p1",
        "classification": "call_for_papers",
        "title": "ICA",
        "description": "submit",
        "date": None,
    }

    tg.notify_missing_date(event, pending_file=pending_file)

    assert pending_file.exists(), "pending_events.json was not created"
    stored = json.loads(pending_file.read_text(encoding="utf-8"))
    assert "42" in stored, f"Expected message_id 42 in pending file, got keys: {list(stored.keys())}"
    entry = stored["42"]
    assert entry.get("classification") == "call_for_papers", (
        f"classification not persisted; stored entry: {entry}"
    )
    assert entry.get("title") == "ICA"


def test_notify_deadline_reminder_noop_without_chat(monkeypatch):
    calls = []
    monkeypatch.setattr(tg, "_token", lambda: "TESTTOKEN")
    monkeypatch.setattr(tg, "_chat_id", lambda: None)
    monkeypatch.setattr(tg, "_api", lambda *a, **k: calls.append(1))
    tg.notify_deadline_reminder({"title": "X", "deadline": "2026-01-01", "days_left": 3})
    assert calls == []
