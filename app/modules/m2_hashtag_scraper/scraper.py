"""
M2 Hashtag Scraper — main processing.

For each active hashtag:
1. Fetch top (50 posts) + recent (100 posts) sections
2. Extract every unique author
3. Upsert into accounts (discovered_via='hashtag', discovered_from=tag)
4. Enqueue authors not yet in accounts into crawl_queue at depth=0
   (they become potential seeds for M1's tagged crawl too)
"""
from __future__ import annotations
import random
from datetime import datetime, timezone, timedelta
from typing import Optional
from app.core.logging import get_logger
from app.core.supabase_client import get_supabase
from app.core.ig_session import ig_session, human_sleep, detect_soft_block
from app.core.rate_limiter import (
    check_and_consume, mark_soft_block,
    get_active_accounts, RateLimitExceeded, AccountUnavailable,
)
from app.modules.m1_tagged_crawler.ig_api import (
    get_profile, normalize_profile, IGRateLimited, IGAPIError,
)
from app.modules.m1_tagged_crawler.repository import upsert_account, account_exists
from app.modules.m1_tagged_crawler.queue import enqueue
from app.core.test_mode import at_test_limit, accounts_remaining
from .ig_api import get_hashtag_posts, extract_post_author

log = get_logger(__name__)

TOP_LIMIT = 50
RECENT_LIMIT = 100
HASHTAG_REFRESH_HOURS = 24  # don't re-scrape same tag more often than this


def _pick_ig_account() -> Optional[dict]:
    accounts = get_active_accounts()
    return random.choice(accounts) if accounts else None


def get_due_hashtags() -> list[dict]:
    """Active hashtags that haven't been scraped recently."""
    sb = get_supabase()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=HASHTAG_REFRESH_HOURS)).isoformat()
    r = (sb.table("hashtags")
         .select("*")
         .eq("active", True)
         .or_(f"last_scraped_at.is.null,last_scraped_at.lt.{cutoff}")
         .order("last_scraped_at", desc=False, nullsfirst=True)
         .limit(5)
         .execute())
    return r.data or []


def mark_hashtag_scraped(hashtag_id: str) -> None:
    get_supabase().table("hashtags").update({
        "last_scraped_at": datetime.now(timezone.utc).isoformat()
    }).eq("id", hashtag_id).execute()


async def scrape_one(tag_row: dict) -> int:
    """
    Scrape one hashtag, return count of new accounts added.
    """
    tag = tag_row["tag"]
    tag_id = tag_row["id"]
    bound_log = log.bind(tag=tag)

    if at_test_limit():
        bound_log.info("m2.test_mode.skip_entire_scrape")
        return 0

    bound_log.info("m2.scrape.start")

    acct = _pick_ig_account()
    if not acct:
        bound_log.error("m2.no_active_accounts")
        return 0
    ig_handle = acct["handle"]

    try:
        check_and_consume(ig_handle, "hashtag_pages", 2)  # top + recent
    except (RateLimitExceeded, AccountUnavailable) as e:
        bound_log.warning("m2.rate_limit", err=str(e))
        return 0

    new_accounts = 0
    all_authors: dict[str, dict] = {}

    async with ig_session(ig_handle, headless=True) as page:
        await page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
        await human_sleep(3, 7)

        for section, limit in [("top", TOP_LIMIT), ("recent", RECENT_LIMIT)]:
            try:
                posts = await get_hashtag_posts(page, tag, section=section, max_posts=limit)
            except IGRateLimited:
                bound_log.warning("m2.rate_limited_by_ig", section=section)
                mark_soft_block(ig_handle)
                return new_accounts
            except IGAPIError as e:
                bound_log.warning("m2.section_error", section=section, err=str(e))
                continue

            for post in posts:
                author = extract_post_author(post)
                if author and author["username"] not in all_authors:
                    all_authors[author["username"]] = author

            await human_sleep(5, 15)

            if await detect_soft_block(page):
                bound_log.warning("m2.soft_block_detected")
                mark_soft_block(ig_handle)
                return new_accounts

        bound_log.info("m2.authors_found", count=len(all_authors))

        # Fetch full profile for each author and upsert (capped by test budget)
        budget = accounts_remaining()
        for handle, author_brief in all_authors.items():
            if budget <= 0:
                bound_log.info("m2.test_mode.budget_exhausted")
                break
            if account_exists(handle):
                continue

            try:
                check_and_consume(ig_handle, "profile_loads", 1)
            except (RateLimitExceeded, AccountUnavailable):
                bound_log.warning("m2.profile_loads_exhausted")
                break

            try:
                user = await get_profile(page, handle)
                normalized = normalize_profile(user)
                upsert_account(normalized,
                               discovered_via="hashtag",
                               discovered_from=tag,
                               depth=0)
                # Also enqueue for tagged-photo crawl
                enqueue(handle, depth=0, parent_seed=f"#{tag}", priority=6)
                new_accounts += 1
                budget -= 1
            except IGRateLimited:
                bound_log.warning("m2.rate_limited_on_profile")
                mark_soft_block(ig_handle)
                break
            except IGAPIError as e:
                bound_log.debug("m2.profile_skip", handle=handle, err=str(e))

            await human_sleep()

    mark_hashtag_scraped(tag_id)
    bound_log.info("m2.scrape.done", new_accounts=new_accounts)
    return new_accounts
