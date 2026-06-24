"""
M1 Tagged Photo Crawler — main processing logic.

For one queued handle:
1. Open IG session (rotate among active operator accounts)
2. Fetch profile metadata, upsert into accounts
3. Fetch tagged-photos feed
4. For each tagged user not already in accounts:
   - Insert tag_edge
   - Enqueue with depth+1 (if under max_depth)

Rate limiting: every action goes through rate_limiter.check_and_consume().
Soft-block detection: any "action blocked" response → cooldown the account.
"""
from __future__ import annotations
import random
from typing import Optional
from app.core.logging import get_logger
from app.core.ig_session import ig_session, human_sleep, detect_soft_block
from app.core.rate_limiter import (
    check_and_consume, mark_soft_block,
    get_active_accounts, RateLimitExceeded, AccountUnavailable,
)
from .ig_api import (
    get_profile, get_tagged_posts,
    extract_tagged_users, extract_post_url, normalize_profile,
    IGRateLimited, IGNotFound, IGAPIError,
)
from .repository import upsert_account, insert_tag_edge
from .queue import enqueue, mark_done, mark_failed
from app.core.test_mode import at_test_limit, accounts_remaining

log = get_logger(__name__)

MAX_DEPTH = 2          # how many hops from a seed
MAX_TAGGED_POSTS = 30  # per profile, to bound work


def _pick_ig_account() -> Optional[dict]:
    accounts = get_active_accounts()
    if not accounts:
        return None
    return random.choice(accounts)


async def process_one(queue_row: dict) -> None:
    """
    Process a single claimed crawl_queue row.

    queue_row keys: id, handle, depth, parent_seed, attempts
    """
    handle = queue_row["handle"]
    depth = queue_row["depth"]
    parent = queue_row.get("parent_seed")
    queue_id = queue_row["id"]

    bound_log = log.bind(handle=handle, depth=depth, queue_id=queue_id)
    bound_log.info("m1.process.start")

    # ----- Test mode check: stop discovering new accounts once at limit -----
    # Note: we still process the seed itself (depth=0) — the limit only blocks
    # recursion into new handles, not refreshing already-known ones.
    if at_test_limit() and depth > 0:
        bound_log.info("m1.test_mode.skip_recursive")
        mark_done(queue_id)
        return

    acct = _pick_ig_account()
    if not acct:
        bound_log.error("m1.no_active_accounts")
        mark_failed(queue_id, "no active IG accounts", retry=True)
        return

    ig_handle = acct["handle"]

    # ----- Rate limit check (profile load) -----
    try:
        check_and_consume(ig_handle, "profile_loads", 1)
    except (RateLimitExceeded, AccountUnavailable) as e:
        bound_log.warning("m1.rate_limit", err=str(e))
        mark_failed(queue_id, str(e), retry=True)
        return

    discovered_via = "manual" if depth == 0 else "tagged"

    async with ig_session(ig_handle, headless=True) as page:
        # First navigate so cookies/anti-bot warm up
        await page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
        await human_sleep(3, 7)

        # ----- Fetch profile -----
        try:
            user = await get_profile(page, handle)
        except IGNotFound:
            bound_log.info("m1.profile.not_found")
            mark_done(queue_id)
            return
        except IGRateLimited:
            bound_log.warning("m1.rate_limited_by_ig")
            mark_soft_block(ig_handle)
            mark_failed(queue_id, "IG rate limited", retry=True)
            return
        except IGAPIError as e:
            bound_log.warning("m1.profile.api_error", err=str(e))
            mark_failed(queue_id, str(e), retry=True)
            return

        normalized = normalize_profile(user)
        user_pk = normalized["_pk"]
        upsert_account(normalized, discovered_via=discovered_via,
                       discovered_from=parent, depth=depth)

        await human_sleep()

        # ----- Stop at max depth (still record the profile, don't recurse) -----
        if depth >= MAX_DEPTH:
            bound_log.info("m1.max_depth_reached")
            mark_done(queue_id)
            return

        # ----- Soft block check -----
        if await detect_soft_block(page):
            bound_log.warning("m1.soft_block_detected")
            mark_soft_block(ig_handle)
            mark_failed(queue_id, "soft block detected", retry=True)
            return

        # ----- Fetch tagged posts -----
        try:
            posts = await get_tagged_posts(page, user_pk, max_posts=MAX_TAGGED_POSTS)
        except IGRateLimited:
            mark_soft_block(ig_handle)
            mark_failed(queue_id, "rate limited on tagged", retry=True)
            return
        except IGAPIError as e:
            bound_log.warning("m1.tagged.error", err=str(e))
            # Tagged feed may be private — that's fine, mark done
            mark_done(queue_id)
            return

        # ----- Extract tagged users, dedupe within this batch -----
        seen = set()
        new_handles: list[tuple[str, int, str]] = []

        for post in posts:
            post_url = extract_post_url(post)
            for tu in extract_tagged_users(post):
                target = tu["username"].lower()
                if target == handle or target in seen:
                    continue
                seen.add(target)

                insert_tag_edge(handle, target, post_url)

                # Only enqueue if we're not at max depth and haven't seen them
                from .repository import account_exists
                if not account_exists(target):
                    new_handles.append((target, depth + 1, handle))

        # ----- Bulk enqueue (respecting test mode budget) -----
        budget = accounts_remaining()
        added = 0
        for h, d, p in new_handles:
            if budget <= 0:
                break
            if enqueue(h, depth=d, parent_seed=p, priority=5 + d):
                added += 1
                budget -= 1

        bound_log.info("m1.process.done",
                       tagged_posts=len(posts),
                       new_handles_found=len(new_handles),
                       newly_queued=added,
                       remaining_budget=accounts_remaining())
        mark_done(queue_id)
