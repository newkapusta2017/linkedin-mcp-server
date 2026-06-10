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
from pipeline.telegram import notify_created, notify_missing_date, process_replies

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


async def _scrape_feed() -> list[dict]:
    """Scrape the LinkedIn feed using standalone Patchright scraper."""
    from pipeline.scraper import scrape_feed

    return await scrape_feed(num_posts=10, headless=True, saved=True)


def _run_pipeline(posts: list[dict], *, dry_run: bool = False) -> int:
    """Run dedup → classify → calendar.  Returns the number of events created."""
    new_posts = get_new_saved_posts(posts)
    if not new_posts:
        logger.info("No new posts to process")
        return 0

    logger.info("Processing %d new post(s)", len(new_posts))
    for i, p in enumerate(new_posts, 1):
        logger.info(
            "Post %d [%s]: %.500s",
            i,
            p.get("post_id", "?"),
            p.get("text", "").replace("\n", " "),
        )

    seen_texts = set()
    unique_posts = []
    for p in new_posts:
        t = p.get("text", "")
        if t not in seen_texts:
            seen_texts.add(t)
            unique_posts.append(p)

    classifications = classify_posts(unique_posts, dry_run=dry_run)
    events = [c for c in classifications if c["classification"] != "none"]

    if not events:
        logger.info("No events found in new posts")
        return 0

    if dry_run:
        logger.info("Dry-run: would create %d calendar event(s):", len(events))
        for e in events:
            logger.info(
                "  → %s | %s | %s %s–%s",
                e.get("classification"),
                e.get("title"),
                e.get("date") or "no date",
                e.get("start_time") or "",
                e.get("end_time") or "",
            )
        return len(events)

    with_date = [e for e in events if e.get("date")]
    without_date = [e for e in events if not e.get("date")]

    created_count = 0
    if with_date:
        logger.info("Creating %d calendar event(s) with dates", len(with_date))
        created = create_events(with_date)
        created_count = len(created)
        for ev, cal_ev in zip(with_date, created):
            notify_created(ev, cal_ev.get("htmlLink"))

    for ev in without_date:
        logger.info("Event without date — sending Telegram prompt: %s", ev.get("title"))
        notify_missing_date(ev)

    return created_count


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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Use keyword classifier and skip calendar creation (no API keys needed)",
    )
    parser.add_argument(
        "--heartbeat",
        action="store_true",
        help="Just visit LinkedIn to keep session alive, then exit",
    )
    parser.add_argument(
        "--bot",
        action="store_true",
        help="Run Telegram bot loop to handle date replies for pending events",
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

    if args.heartbeat:
        from pipeline.scraper import heartbeat

        alive = asyncio.run(heartbeat())
        raise SystemExit(0 if alive else 1)

    if args.bot:
        from pipeline.telegram import run_bot_loop

        run_bot_loop()
        return

    # Process any pending date replies from previous runs
    reply_events = process_replies()
    if reply_events:
        logger.info("Processing %d pending date replies", len(reply_events))
        created = create_events(reply_events)
        for ev, cal_ev in zip(reply_events, created):
            notify_created(ev, cal_ev.get("htmlLink"))
        logger.info("Created %d calendar events from replies", len(created))

    run_count = 0

    while True:
        run_count += 1
        logger.info("=== Pipeline run #%d ===", run_count)

        try:
            if args.input:
                posts = _load_posts_from_file(args.input)
                logger.info("Loaded %d posts from %s", len(posts), args.input)
            else:
                logger.info("Scraping LinkedIn feed")
                posts = asyncio.run(_scrape_feed())
                logger.info("Scraped %d posts from feed", len(posts))

            created = _run_pipeline(posts, dry_run=args.dry_run)
            logger.info("Run #%d complete — %d event(s) created", run_count, created)

        except Exception:
            logger.exception("Run #%d failed", run_count)

        if not args.loop:
            break

        logger.info("Sleeping %d seconds until next run", args.interval)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
