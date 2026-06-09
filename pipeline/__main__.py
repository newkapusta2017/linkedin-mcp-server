"""
Scheduler entry point for the LinkedIn saved-posts → calendar pipeline.

Usage:
    python -m pipeline                         # single run
    python -m pipeline --loop                  # continuous (default: every 24h)
    python -m pipeline --loop --interval 3600  # continuous, hourly
    python -m pipeline --input posts.json      # skip scraping, process from file

Cron example (daily at 9 AM):
    0 9 * * * cd /path/to/project && python -m pipeline >> /var/log/pipeline.log 2>&1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path

from dotenv import load_dotenv

from pipeline.calendar import create_events
from pipeline.classifier import classify_posts
from pipeline.state import get_new_saved_posts

logger = logging.getLogger("pipeline")

DEFAULT_INTERVAL = 86400  # 24 hours


def _load_posts_from_file(path: str) -> list[dict]:
    """Read saved-post JSON from a file (tool output or manual export)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "posts" in data:
        return data["posts"]
    if isinstance(data, list):
        return data
    raise ValueError(f"Unexpected JSON format in {path}")


async def _scrape_saved_posts() -> list[dict]:
    """Call the MCP server's get_saved_posts tool in-process."""
    from fastmcp import Client

    from linkedin_mcp_server.server import mcp

    async with Client(mcp) as client:
        result = await client.call_tool("get_saved_posts")

    for content in result:
        if hasattr(content, "text"):
            data = json.loads(content.text)
            return data.get("posts", [])

    logger.warning("get_saved_posts returned no text content")
    return []


def _run_pipeline(posts: list[dict]) -> int:
    """Run dedup → classify → calendar.  Returns the number of events created."""
    new_posts = get_new_saved_posts(posts)
    if not new_posts:
        logger.info("No new posts to process")
        return 0

    logger.info("Processing %d new post(s)", len(new_posts))

    classifications = classify_posts(new_posts)
    events = [c for c in classifications if c["classification"] != "none"]

    if not events:
        logger.info("No events found in new posts")
        return 0

    logger.info("Found %d event(s), creating calendar entries", len(events))
    created = create_events(events)
    return len(created)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline",
        description="LinkedIn saved-posts → calendar pipeline",
    )
    parser.add_argument(
        "--input",
        metavar="FILE",
        help="Read posts from a JSON file instead of scraping LinkedIn",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously instead of exiting after one pass",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL,
        help=f"Seconds between runs in --loop mode (default: {DEFAULT_INTERVAL})",
    )
    return parser.parse_args()


def main() -> None:
    load_dotenv()

    args = _parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    run_count = 0

    while True:
        run_count += 1
        logger.info("=== Pipeline run #%d ===", run_count)

        try:
            if args.input:
                posts = _load_posts_from_file(args.input)
                logger.info("Loaded %d posts from %s", len(posts), args.input)
            else:
                logger.info("Scraping saved posts from LinkedIn")
                posts = asyncio.run(_scrape_saved_posts())
                logger.info("Scraped %d posts", len(posts))

            created = _run_pipeline(posts)
            logger.info("Run #%d complete — %d event(s) created", run_count, created)

        except Exception:
            logger.exception("Run #%d failed", run_count)

        if not args.loop:
            break

        logger.info("Sleeping %d seconds until next run", args.interval)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
