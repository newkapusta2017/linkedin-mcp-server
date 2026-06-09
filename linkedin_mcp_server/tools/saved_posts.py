"""
LinkedIn saved-posts scraping tool.

Fetches the authenticated user's saved posts from
https://www.linkedin.com/my-items/saved-posts/ using innerText extraction
and minimal DOM selectors.  Returns structured JSON with per-post metadata
so downstream consumers (classification, calendar pipeline) can process
posts without further LLM parsing.
"""

import hashlib
import logging
from typing import Any

from fastmcp import Context, FastMCP

from linkedin_mcp_server.common_utils import utcnow_iso
from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DOM selectors — isolated here because LinkedIn changes its HTML frequently.
# Update these constants when scraping breaks.
# ---------------------------------------------------------------------------

# Each saved post is rendered as a card inside the saved-items list.
# The reusable-search result container wraps individual saved items.
SAVED_POST_CARD_SELECTOR = "div.reusable-search__result-container"

# Within each card, the link to the original post or article.
POST_LINK_SELECTOR = "a.app-aware-link"

# The author/source name — entity-result title area.
AUTHOR_NAME_SELECTOR = "span.entity-result__title-text"

# The post body / snippet preview.
POST_TEXT_SELECTOR = "div.entity-result__summary"

# Fallback: if the above selectors fail, try these broader alternatives.
# LinkedIn sometimes uses different class names across A/B test variants.
FALLBACK_CARD_SELECTOR = "li.reusable-search__result-container"
FALLBACK_AUTHOR_SELECTOR = ".entity-result__title-text a span[aria-hidden='true']"

SAVED_POSTS_URL = "https://www.linkedin.com/my-items/saved-posts/"


def _derive_post_id(url: str) -> str:
    """Derive a stable, deterministic post_id from the post URL."""
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def register_saved_posts_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register saved-posts tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Saved Posts",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"saved_posts", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_saved_posts(
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Get the authenticated user's saved LinkedIn posts.

        Navigates to the saved-posts page and extracts each saved item
        as structured JSON.

        Returns:
            Dict with:
            - url: the saved-posts page URL
            - posts: list of {post_id, author, text, post_url, scraped_at}
            - section_errors: present when extraction fails or page is empty
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_saved_posts"
            )
            logger.info("Scraping saved posts")

            await ctx.report_progress(
                progress=0, total=100, message="Navigating to saved posts"
            )

            await extractor._navigate_to_page(SAVED_POSTS_URL)

            await ctx.report_progress(
                progress=20, total=100, message="Page loaded, waiting for content"
            )

            page = extractor._page

            # Wait for the main content area to render.
            try:
                await page.wait_for_selector("main", timeout=10000)
            except Exception:
                logger.debug("No <main> element on saved-posts page")

            # Dismiss any modals that may overlay the content.
            from linkedin_mcp_server.core.utils import handle_modal_close

            await handle_modal_close(page)

            # Scroll down to load lazy-rendered saved items.
            from linkedin_mcp_server.core.utils import scroll_to_bottom

            await scroll_to_bottom(page, max_scrolls=5)

            await ctx.report_progress(
                progress=50, total=100, message="Extracting saved posts"
            )

            # Try primary selectors, fall back to alternatives.
            cards = await page.query_selector_all(SAVED_POST_CARD_SELECTOR)
            if not cards:
                cards = await page.query_selector_all(FALLBACK_CARD_SELECTOR)

            if not cards:
                # No cards found — might be empty or selectors are stale.
                # Extract raw page text as a diagnostic fallback.
                raw_text = await page.evaluate(
                    "() => (document.querySelector('main') || document.body).innerText || ''"
                )
                logger.warning(
                    "No saved-post cards found; raw page length=%d", len(raw_text)
                )
                return {
                    "url": SAVED_POSTS_URL,
                    "posts": [],
                    "section_errors": {
                        "saved_posts": {
                            "error_type": "no_results",
                            "error_message": (
                                "No saved posts found. The page may be empty, "
                                "or LinkedIn has changed its HTML structure. "
                                "Update the selectors in saved_posts.py."
                            ),
                            "raw_text_preview": raw_text[:500],
                        }
                    },
                }

            scraped_at = utcnow_iso()
            posts: list[dict[str, str]] = []

            for card in cards:
                post_url = ""
                author = ""
                text = ""

                # Extract the first meaningful link (the post permalink).
                links = await card.query_selector_all(POST_LINK_SELECTOR)
                for link in links:
                    href = await link.get_attribute("href")
                    if href and (
                        "/feed/" in href or "/posts/" in href or "/pulse/" in href
                    ):
                        post_url = href.split("?")[0]
                        break
                # If no post-specific link, take the first link as fallback.
                if not post_url and links:
                    href = await links[0].get_attribute("href")
                    if href:
                        post_url = href.split("?")[0]

                # Extract author name.
                author_el = await card.query_selector(AUTHOR_NAME_SELECTOR)
                if not author_el:
                    author_el = await card.query_selector(FALLBACK_AUTHOR_SELECTOR)
                if author_el:
                    author = (await author_el.inner_text()).strip()

                # Extract post text/snippet.
                text_el = await card.query_selector(POST_TEXT_SELECTOR)
                if text_el:
                    text = (await text_el.inner_text()).strip()

                # Fall back to the card's full innerText if text is empty.
                if not text:
                    card_text = (await card.inner_text()).strip()
                    # Use the card text minus the author name to avoid duplication.
                    if author and card_text.startswith(author):
                        text = card_text[len(author) :].strip()
                    else:
                        text = card_text

                if not post_url:
                    logger.debug("Skipping card with no extractable URL")
                    continue

                posts.append(
                    {
                        "post_id": _derive_post_id(post_url),
                        "author": author,
                        "text": text,
                        "post_url": post_url,
                        "scraped_at": scraped_at,
                    }
                )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            logger.info("Extracted %d saved posts", len(posts))

            result: dict[str, Any] = {
                "url": SAVED_POSTS_URL,
                "posts": posts,
            }
            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_saved_posts")
        except Exception as e:
            raise_tool_error(e, "get_saved_posts")
