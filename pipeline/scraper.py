"""
Standalone LinkedIn feed scraper using Patchright.

Bypasses the MCP server entirely — launches a browser with the stored
profile, navigates to the feed, and extracts individual posts.

LinkedIn uses hashed/obfuscated CSS class names, so we can't rely on
stable selectors. Instead we:
  1. Find profile links (a[href*="/in/"]) as post author anchors
  2. Walk up the DOM to find the enclosing post container
  3. Extract post text, author, and URL from each container

Usage:
    # Playground — opens visible browser, prints posts:
    python -m pipeline.scraper
    python -m pipeline.scraper --saved

    # Programmatic:
    from pipeline.scraper import scrape_feed
    posts = asyncio.run(scrape_feed())
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

PROFILE_DIR = Path.home() / ".linkedin-mcp" / "profile"
FEED_URL = "https://www.linkedin.com/feed/"
SAVED_URL = "https://www.linkedin.com/my-items/saved-posts/"

# JS that finds posts by locating profile links, then walking up to
# the post container boundary.  Works regardless of class name hashing.
EXTRACT_POSTS_JS = """
() => {
    const posts = [];
    const seen = new Set();
    const main = document.querySelector('main') || document.body;

    // Find all profile links in the feed area.
    // Exclude sidebar (left) and right rail — only look at the center column.
    const profileLinks = main.querySelectorAll('a[href*="/in/"]');

    // Collect the current user's profile slug to filter sidebar links.
    // Also check the sidebar profile card link directly.
    const selfLinks = new Set();
    const selfCandidates = document.querySelectorAll(
        'nav a[href*="/in/"], header a[href*="/in/"], ' +
        'aside a[href*="/in/"], [class*="profile"] a[href*="/in/"]'
    );
    for (const a of selfCandidates) {
        const href = new URL(a.href, location.origin).pathname;
        selfLinks.add(href.replace(/\\/$/, ''));
    }
    // Also grab from the "Me" menu / profile image link at top
    const meLink = document.querySelector('a[href*="/in/"][class*="ember"], img[alt]');
    if (meLink && meLink.closest('a')) {
        const href = new URL(meLink.closest('a').href, location.origin).pathname;
        selfLinks.add(href.replace(/\\/$/, ''));
    }

    for (const link of profileLinks) {
        // Skip links to self (sidebar / compose box / profile card)
        const linkPath = new URL(link.href, location.origin).pathname.replace(/\\/$/, '');
        if (selfLinks.has(linkPath)) continue;

        // Skip links that are outside the feed area (sidebar, right rail)
        // The feed is in the center — skip if the link is in an aside
        if (link.closest('aside, nav, header, footer')) continue;

        // Walk up to find the post container
        let container = link;
        for (let i = 0; i < 15; i++) {
            const parent = container.parentElement;
            if (!parent || parent === main || parent === document.body) break;

            const siblingsWithLinks = Array.from(parent.children).filter(c =>
                c !== container && c.querySelector('a[href*="/in/"]')
            );
            if (siblingsWithLinks.length > 0) break;

            container = parent;
        }

        const fullText = (container.innerText || '').trim();
        if (!fullText || fullText.length < 50) continue;

        // Deduplicate by container
        const sig = fullText.substring(0, 200);
        if (seen.has(sig)) continue;
        seen.add(sig);

        // Author: the profile link's visible text
        const author = link.innerText.trim().split('\\n')[0].trim();

        // Post URL
        let url = '';
        const postLink = container.querySelector(
            'a[href*="/feed/update/"], a[href*="/posts/"]'
        );
        if (postLink) url = postLink.href;

        const urnEl = container.querySelector('[data-urn]');
        const urn = urnEl ? urnEl.getAttribute('data-urn') : '';

        // Extract the post body: strip author info and engagement chrome.
        // Split on timestamp pattern, take everything after it until
        // engagement buttons (Gefällt mir, Like, Kommentar, etc.)
        const tsMatch = fullText.match(
            /\\d+\\s*(?:Std|Tag|Min|Wo(?:che)?|Monat|Sek|hr|day|min|wk|mo|sec)[^\\n]*/
        );
        let bodyText = fullText;
        if (tsMatch) {
            const afterTs = fullText.substring(
                fullText.indexOf(tsMatch[0]) + tsMatch[0].length
            );
            // Strip "Folgen"/"Follow" at start
            const stripped = afterTs.replace(/^\\s*(?:Folgen|Follow)\\s*\\n?/, '').trim();
            if (stripped.length > 30) bodyText = stripped;
        }

        // Cut off engagement metrics
        const engageMatch = bodyText.match(
            /\\n\\s*(?:\\d+\\s+)?(?:Gefällt mir|Kommentar|Like|Comment|Repost|Teilen|Share|Senden|Send)\\b/
        );
        if (engageMatch) {
            bodyText = bodyText.substring(0, engageMatch.index).trim();
        }

        if (bodyText.length < 30) continue;

        // Skip "recommended people" widgets and pure author cards
        if (/^(?:Feed-Beitrag|Für Sie empfohlen|Suggested for you)/i.test(bodyText)) continue;
        if (/^Folgen\\s*$/m.test(bodyText) && bodyText.length < 100) continue;

        posts.push({ text: bodyText, author, url, urn: urn || '' });
    }

    return posts;
}
"""

# German/English timestamp pattern that separates author info from post text
_TIMESTAMP_RE = re.compile(
    r"\n\s*\d+\s*(?:Std\.|Tag(?:\(e\))?|Min\.|Wo\.|Monat|"
    r"hr|day|min|wk|mo)\.?\s*(?:·\s*)?\n",
    re.IGNORECASE,
)

# "Folgen" / "Follow" button text (appears after author block)
_FOLLOW_RE = re.compile(r"^\s*(?:Folgen|Follow)\s*$", re.MULTILINE)


def _parse_feed_text(raw_text: str) -> list[dict]:
    """Parse the full feed innerText into individual posts.

    Fallback when DOM selectors find nothing.  Splits on timestamp
    patterns that separate author info from post body.
    """
    # Strip everything before "Feed-Beitrag" / "Feed post" marker
    feed_start = re.search(
        r"(?:Feed-Beitrag|Feed post|Relevanteste zuerst|Top results first)",
        raw_text,
    )
    if feed_start:
        raw_text = raw_text[feed_start.end() :]

    # Split on timestamp markers (each post starts with author + timestamp)
    parts = _TIMESTAMP_RE.split(raw_text)
    if len(parts) < 2:
        return []

    posts = []
    for i in range(1, len(parts)):
        chunk = parts[i].strip()
        # Remove "Folgen"/"Follow" button text at start
        chunk = _FOLLOW_RE.sub("", chunk, count=1).strip()

        if not chunk or len(chunk) < 30:
            continue

        # The previous chunk's last lines are the author info for this post
        prev = parts[i - 1].strip()
        prev_lines = [l.strip() for l in prev.split("\n") if l.strip()]

        # Author is typically the last substantial line before the timestamp
        author = ""
        for line in reversed(prev_lines):
            if len(line) > 2 and not line.startswith(("·", "Vorgeschlagen")):
                author = line
                break

        # Cut off at engagement metrics (Gefällt mir, Kommentar, Like, etc.)
        end_markers = re.search(
            r"\n\s*(?:\d+\s+)?(?:Gefällt mir|Kommentar|Like|Comment|Repost|"
            r"Beitrag teilen|Share|Senden|Send)\b",
            chunk,
        )
        body = chunk[: end_markers.start()] if end_markers else chunk
        body = body.strip()

        if len(body) < 20:
            continue

        post_id = hashlib.sha256(body[:200].encode()).hexdigest()[:16]
        posts.append(
            {
                "post_id": post_id,
                "author": author,
                "text": body,
                "post_url": "",
            }
        )

    return posts


async def scrape_feed(
    num_posts: int = 10,
    headless: bool = True,
    saved: bool = False,
) -> list[dict]:
    """Scrape LinkedIn feed or saved-posts page.

    Returns a list of dicts with keys: post_id, author, text, post_url.
    """
    from patchright.async_api import async_playwright

    profile = str(PROFILE_DIR)
    if not PROFILE_DIR.exists():
        raise RuntimeError(
            f"No LinkedIn profile at {PROFILE_DIR}. "
            "Run: uv run python -m linkedin_mcp_server --login"
        )

    url = SAVED_URL if saved else FEED_URL
    label = "saved posts" if saved else "feed"

    logger.info("Launching browser (headless=%s)", headless)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch_persistent_context(
            user_data_dir=profile,
            headless=headless,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()

        logger.info("Navigating to %s", url)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            logger.error("Navigation failed: %s", e)
            await browser.close()
            return []

        if "/login" in page.url:
            logger.error(
                "Redirected to login (%s) — session expired. "
                "Run: uv run python -m linkedin_mcp_server --login",
                page.url,
            )
            await browser.close()
            return []

        logger.info("Page loaded: %s", page.url)

        # Wait for feed to render
        await page.wait_for_timeout(4000)

        # Scroll to load more posts
        for scroll in range(max(num_posts // 2, 4)):
            await page.mouse.wheel(0, 1500)
            await page.wait_for_timeout(2000)

        # Try DOM-based extraction first
        raw_posts = await page.evaluate(EXTRACT_POSTS_JS)
        logger.info(
            "DOM extraction: %d post candidates from %s", len(raw_posts), label
        )

        # If DOM found nothing, fall back to innerText parsing
        if not raw_posts:
            logger.info("DOM selectors found no posts — trying text parser")
            main_text = await page.evaluate(
                "() => (document.querySelector('main') || document.body).innerText"
            )
            logger.info("Feed innerText: %d chars", len(main_text))
            raw_posts = _parse_feed_text(main_text)
            logger.info("Text parser found %d posts", len(raw_posts))

            if not raw_posts and not headless:
                logger.info(
                    "No posts found. Browser is open — inspect the page, "
                    "then close it."
                )
                try:
                    await page.wait_for_event("close", timeout=300000)
                except Exception:
                    pass

            await browser.close()
            return raw_posts[:num_posts]

        await browser.close()

    posts = []
    seen_texts = set()
    for p in raw_posts:
        text = p.get("text", "")
        if not text or len(text) < 30:
            continue
        sig = text[:200]
        if sig in seen_texts:
            continue
        seen_texts.add(sig)

        url = p.get("url", "")
        urn = p.get("urn", "")
        key = url or urn or text[:200]
        post_id = hashlib.sha256(key.encode()).hexdigest()[:16]
        posts.append(
            {
                "post_id": post_id,
                "author": p.get("author", ""),
                "text": text,
                "post_url": url,
            }
        )

    return posts[:num_posts]


async def playground(saved: bool = False) -> None:
    """Open a visible browser, scrape, and print results."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    posts = await scrape_feed(num_posts=10, headless=False, saved=saved)
    print(f"\n{'=' * 60}")
    print(f"Found {len(posts)} posts")
    print(f"{'=' * 60}\n")
    for i, p in enumerate(posts, 1):
        print(f"--- Post {i} [{p['post_id']}] ---")
        print(f"Author: {p['author'] or '(unknown)'}")
        print(f"URL:    {p['post_url'] or '(none)'}")
        print(f"Text:   {p['text'][:300]}")
        print()


if __name__ == "__main__":
    asyncio.run(playground(saved="--saved" in sys.argv))
