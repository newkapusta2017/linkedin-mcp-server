"""
Post classifier for the saved-posts pipeline.

Calls the Anthropic API to classify each post as ``invitation``,
``save_the_date``, or ``none``, and extracts structured event details
when applicable.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import anthropic

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """\
You are a classifier for LinkedIn posts.  For each post you receive,
decide whether it is an **invitation** to an event, a **save_the_date**
announcement, or **none** of the above.

Respond with a single JSON object — no markdown fences, no commentary.

Schema:

{
  "classification": "invitation" | "save_the_date" | "none",
  "title": "string or null",
  "date": "ISO 8601 date or null",
  "start_time": "HH:MM or null",
  "end_time": "HH:MM or null",
  "location": "string or null",
  "description": "one-line summary or null"
}

Rules:
- "invitation": the post explicitly invites the reader to attend an event.
- "save_the_date": the post announces an upcoming event without a direct
  invitation (e.g. a conference teaser, a date announcement).
- "none": everything else — job posts, articles, personal updates, etc.
- When classification is "none", set all other fields to null.
- Dates must be ISO 8601 (YYYY-MM-DD).  If only a month or season is
  given, use the first day of that month/season.
- Times are 24-hour format.  If not mentioned, set to null.
- Keep "description" to one sentence.
"""

NONE_RESULT: dict[str, Any] = {
    "classification": "none",
    "title": None,
    "date": None,
    "start_time": None,
    "end_time": None,
    "location": None,
    "description": None,
}


def _get_client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY environment variable is not set. "
            "Set it before running the pipeline."
        )
    return anthropic.Anthropic(api_key=api_key)


def _parse_json(text: str) -> dict[str, Any] | None:
    """Defensively parse JSON from the model response."""
    text = text.strip()
    # Strip markdown fences if the model wraps anyway.
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Failed to parse model response as JSON: %.200s", text)
        return None


def classify_post(post: dict[str, Any]) -> dict[str, Any]:
    """Classify a single post via the Anthropic API.

    Args:
        post: A post dict with at least ``author`` and ``text`` keys.

    Returns:
        A dict with keys: classification, title, date, start_time,
        end_time, location, description.  Falls back to ``"none"``
        classification on any error.
    """
    author = post.get("author", "")
    text = post.get("text", "")

    if not text:
        logger.debug("Skipping post with empty text (post_id=%s)", post.get("post_id"))
        return dict(NONE_RESULT)

    user_message = f"Author: {author}\n\nPost:\n{text}"

    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)

    try:
        client = _get_client()
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
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


def classify_posts(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Classify a list of posts, returning results with post metadata attached.

    Each returned dict contains the original ``post_id`` plus all
    classification fields.  Posts classified as ``"none"`` are included
    so the caller can decide whether to filter them.
    """
    results: list[dict[str, Any]] = []

    for post in posts:
        post_id = post.get("post_id", "unknown")
        logger.info("Classifying post %s", post_id)

        classification = classify_post(post)
        classification["post_id"] = post_id

        logger.info(
            "Post %s → %s%s",
            post_id,
            classification["classification"],
            f" ({classification['title']})" if classification.get("title") else "",
        )

        results.append(classification)

    events = [r for r in results if r["classification"] != "none"]
    logger.info(
        "Classified %d posts: %d events, %d skipped",
        len(results),
        len(events),
        len(results) - len(events),
    )

    return results
