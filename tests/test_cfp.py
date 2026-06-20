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
