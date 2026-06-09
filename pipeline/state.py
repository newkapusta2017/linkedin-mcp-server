"""
Persistent state for the saved-posts pipeline.

Tracks which post_ids have already been processed so the pipeline only
acts on new posts.  Uses a local JSON file for storage, written atomically
to avoid corruption on crash.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_STATE_DIR = Path.home() / ".linkedin-mcp" / "pipeline"
DEFAULT_STATE_FILE = DEFAULT_STATE_DIR / "processed_posts.json"


class PostStore:
    """JSON-backed store of processed post IDs."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or DEFAULT_STATE_FILE
        self._seen: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            logger.info("No state file at %s — starting fresh", self._path)
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._seen = set(data.get("processed_ids", []))
            logger.info(
                "Loaded %d processed post IDs from %s", len(self._seen), self._path
            )
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Could not read state file %s: %s — starting fresh", self._path, exc
            )

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"processed_ids": sorted(self._seen)},
            indent=2,
        )
        # Atomic write: temp file + replace in the same directory.
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def is_seen(self, post_id: str) -> bool:
        return post_id in self._seen

    def mark_seen(self, post_id: str) -> None:
        self._seen.add(post_id)

    def save(self) -> None:
        self._save()

    @property
    def count(self) -> int:
        return len(self._seen)


def get_new_saved_posts(
    posts: list[dict[str, Any]],
    store: PostStore | None = None,
) -> list[dict[str, Any]]:
    """Filter *posts* to only those not yet processed, then mark them as seen.

    Args:
        posts: List of post dicts as returned by the get_saved_posts tool.
               Each must have a "post_id" key.
        store: PostStore instance. Uses the default state file if not provided.

    Returns:
        List of post dicts that have not been processed before.
        The store is updated (and persisted) with the new post IDs.
    """
    if store is None:
        store = PostStore()

    new_posts = [p for p in posts if not store.is_seen(p["post_id"])]

    if not new_posts:
        logger.info("No new posts (all %d already processed)", len(posts))
        return []

    for post in new_posts:
        store.mark_seen(post["post_id"])
    store.save()

    logger.info(
        "Found %d new posts out of %d total (%d previously processed)",
        len(new_posts),
        len(posts),
        store.count - len(new_posts),
    )
    return new_posts
