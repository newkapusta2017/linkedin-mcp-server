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
