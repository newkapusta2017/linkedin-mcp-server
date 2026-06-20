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
