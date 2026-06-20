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
