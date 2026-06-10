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


FIND_SAVED_LINKS_JS = """
() => {
    const links = [];
    const seen = new Set();
    const main = document.querySelector('main') || document.body;

    // Find all links that point to posts or feed updates
    const anchors = main.querySelectorAll(
        'a[href*="/feed/update/"], a[href*="/posts/"]'
    );
    for (const a of anchors) {
        const href = a.href;
        if (seen.has(href)) continue;
        seen.add(href);
        links.push(href);
    }

    // Also try broader: any card/item that has a clickable link
    if (links.length === 0) {
        const allLinks = main.querySelectorAll('a[href]');
        for (const a of allLinks) {
            const href = a.href;
            if (href.includes('/feed/') || href.includes('/posts/')) {
                if (seen.has(href)) continue;
                seen.add(href);
                links.push(href);
            }
        }
    }

    return links;
}
"""

EXTRACT_SINGLE_POST_JS = """
() => {
    const main = document.querySelector('main') || document.body;

    // On a post detail page, find the post author.
    // Skip links to self — look for non-nav profile links.
    let author = '';
    const profileLinks = main.querySelectorAll('a[href*="/in/"]');
    for (const a of profileLinks) {
        // Skip if it's in the sidebar/nav
        if (a.closest('aside, nav, header')) continue;
        const text = a.innerText.trim().split('\\n')[0].trim();
        if (text && text.length > 1 && text.length < 60) {
            author = text;
            break;
        }
    }

    // Post body: the full page innerText, then parse out the post
    const fullText = main.innerText || '';

    // Find the post text by looking for the timestamp marker,
    // then taking everything after "Folgen"/"Follow" until
    // engagement buttons.
    const tsMatch = fullText.match(
        /\\d+\\s*(?:Std|Tag|Min|Wo(?:che)?|Monat|Sek|hr|day|min|wk|mo|sec)[^\\n]*/
    );
    let bodyText = '';
    if (tsMatch) {
        const afterTs = fullText.substring(
            fullText.indexOf(tsMatch[0]) + tsMatch[0].length
        );
        bodyText = afterTs.replace(/^\\s*(?:Folgen|Follow)\\s*\\n?/, '').trim();
    }

    // Cut off engagement metrics
    if (bodyText) {
        const engageMatch = bodyText.match(
            /\\n\\s*(?:\\d+\\s+)?(?:Gefällt mir|Kommentar|Like|Comment|Repost|Teilen|Share|Senden|Send|Reaktion)\\b/
        );
        if (engageMatch) {
            bodyText = bodyText.substring(0, engageMatch.index).trim();
        }
    }

    // Cut off "mehr" / "...mehr" / "more" / "see more" link text
    if (bodyText) {
        const moreMatch = bodyText.match(/\\n\\s*(?:…\\s*)?(?:mehr|more|see more|Übersetzung anzeigen)\\s*$/im);
        if (moreMatch) {
            bodyText = bodyText.substring(0, moreMatch.index).trim();
        }
    }

    // Strip video player chrome that appears at the start
    bodyText = bodyText.replace(
        /^(?:Pause|Play|Skip|Unmute|Mute|Current Time|Duration|Loaded|Stream|Seek|Remaining|Playback|Chapters|Descriptions|Subtitles|Audio|Picture|Fullscreen|LIVE|[0-9:.x%\\-/]+|\\s)+/,
        ''
    ).trim();

    // Fall back to longest span if parsing failed
    if (!bodyText || bodyText.length < 30) {
        const spans = main.querySelectorAll('span[dir="ltr"]');
        let longest = '';
        for (const s of spans) {
            const t = s.innerText.trim();
            if (t.length > longest.length && t.length > 30) longest = t;
        }
        if (longest) bodyText = longest;
    }

    return { author, text: bodyText };
}
"""


async def _launch_browser(headless, profile):
    from patchright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await pw.chromium.launch_persistent_context(
        user_data_dir=profile,
        headless=headless,
        viewport={"width": 1280, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = browser.pages[0] if browser.pages else await browser.new_page()
    return pw, browser, page


async def _navigate(page, url):
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    if "/login" in page.url:
        raise RuntimeError(
            f"Redirected to login ({page.url}) — session expired. "
            "Run: uv run python -m linkedin_mcp_server --login"
        )


async def _scrape_saved_posts(page, num_posts, headless):
    """Scrape saved-posts: get links from listing, visit each post."""
    await page.wait_for_timeout(3000)

    # Scroll to load all saved posts
    for _ in range(3):
        await page.mouse.wheel(0, 1500)
        await page.wait_for_timeout(1500)

    post_links = await page.evaluate(FIND_SAVED_LINKS_JS)
    logger.info("Found %d saved post links", len(post_links))

    if not post_links:
        logger.warning("No saved post links found on page")
        main_text = await page.evaluate(
            "() => (document.querySelector('main') || document.body).innerText"
        )
        logger.info("Page text (%d chars): %.500s", len(main_text), main_text)
        return []

    posts = []
    for i, link in enumerate(post_links[:num_posts]):
        logger.info("Visiting saved post %d/%d: %s", i + 1, len(post_links), link)
        try:
            await _navigate(page, link)
            await page.wait_for_timeout(3000)

            data = await page.evaluate(EXTRACT_SINGLE_POST_JS)
            text = data.get("text", "")
            author = data.get("author", "")

            if text and len(text) > 30:
                post_id = hashlib.sha256(link.encode()).hexdigest()[:16]
                posts.append(
                    {
                        "post_id": post_id,
                        "author": author,
                        "text": text,
                        "post_url": link,
                    }
                )
                logger.info("  → %s: %d chars", author or "(unknown)", len(text))
            else:
                logger.warning("  → post had no content (%d chars)", len(text))
        except Exception as e:
            logger.error("  → failed to load post: %s", e)

    return posts


async def _scrape_feed_posts(page, num_posts):
    """Scrape feed page: extract individual posts via DOM."""
    await page.wait_for_timeout(4000)

    for _ in range(max(num_posts // 2, 4)):
        await page.mouse.wheel(0, 1500)
        await page.wait_for_timeout(2000)

    raw_posts = await page.evaluate(EXTRACT_POSTS_JS)
    logger.info("DOM extraction: %d post candidates", len(raw_posts))

    if not raw_posts:
        logger.info("DOM found no posts — trying text parser")
        main_text = await page.evaluate(
            "() => (document.querySelector('main') || document.body).innerText"
        )
        return _parse_feed_text(main_text)

    return raw_posts


async def scrape_feed(
    num_posts: int = 10,
    headless: bool = True,
    saved: bool = False,
) -> list[dict]:
    """Scrape LinkedIn feed or saved-posts page.

    Returns a list of dicts with keys: post_id, author, text, post_url.
    """
    profile = str(PROFILE_DIR)
    if not PROFILE_DIR.exists():
        raise RuntimeError(
            f"No LinkedIn profile at {PROFILE_DIR}. "
            "Run: uv run python -m linkedin_mcp_server --login"
        )

    url = SAVED_URL if saved else FEED_URL
    label = "saved posts" if saved else "feed"

    logger.info("Launching browser (headless=%s)", headless)

    pw, browser, page = await _launch_browser(headless, profile)
    try:
        logger.info("Navigating to %s", url)
        await _navigate(page, url)
        logger.info("Page loaded: %s", page.url)

        if saved:
            posts = await _scrape_saved_posts(page, num_posts, headless)
        else:
            raw_posts = await _scrape_feed_posts(page, num_posts)
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

                p_url = p.get("url", "")
                urn = p.get("urn", "")
                key = p_url or urn or text[:200]
                post_id = hashlib.sha256(key.encode()).hexdigest()[:16]
                posts.append(
                    {
                        "post_id": post_id,
                        "author": p.get("author", ""),
                        "text": text,
                        "post_url": p_url,
                    }
                )

        return posts[:num_posts]
    except RuntimeError:
        raise
    except Exception as e:
        logger.error("Scraping failed: %s", e)
        return []
    finally:
        await browser.close()
        await pw.stop()


async def heartbeat() -> bool:
    """Visit LinkedIn briefly to keep the session alive.

    Returns True if the session is still valid, False if login is required.
    """
    profile = str(PROFILE_DIR)
    if not PROFILE_DIR.exists():
        logger.warning("No LinkedIn profile at %s", PROFILE_DIR)
        return False

    logger.info("Heartbeat: checking LinkedIn session")
    pw, browser, page = await _launch_browser(headless=True, profile=profile)
    try:
        await page.goto(FEED_URL, wait_until="domcontentloaded", timeout=30000)
        if "/login" in page.url:
            logger.warning("Heartbeat: session expired (redirected to login)")
            return False
        logger.info("Heartbeat: session alive")
        return True
    except Exception as e:
        logger.error("Heartbeat failed: %s", e)
        return False
    finally:
        await browser.close()
        await pw.stop()


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
