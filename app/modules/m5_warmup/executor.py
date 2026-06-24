"""
M5 Warm-up Executor.

Polls warming_actions where status='scheduled' AND scheduled_for <= now.
For each: open the IG session (operator account), dispatch to the right action,
update status to 'executed' or 'failed'.

Rate-limiter integration: each follow / like_post / profile_load consumes
budget via the same check_and_consume() the M1 crawler uses.

When all non-comment actions for a prospect have status='executed',
transition the prospect's qualified_prospects.status to 'warmup_complete'
and set dm_unblock_at = NOW() + 24h.
"""
from __future__ import annotations
import random
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional
from app.core.supabase_client import get_supabase
from app.core.logging import get_logger
from app.core.ig_session import ig_session, human_sleep
from app.core.rate_limiter import check_and_consume, RateLimitExceeded, AccountUnavailable
from app.modules.m1_tagged_crawler.ig_api import IGAPIError, IGRateLimited, IGNotFound
from .ig_actions import (
    follow_user,
    like_media,
    fetch_recent_media_ids,
    resolve_user_pk,
    fetch_active_stories,
    view_story,
    like_story,
)

log = get_logger("m5.executor")


BATCH_SIZE = 5
BETWEEN_ACTIONS_MIN_S = 30
BETWEEN_ACTIONS_MAX_S = 90

# How long to cooldown an IG account after IG returns require_login on an
# authenticated request (soft-block). 2h is enough to let IG's per-account
# heuristics relax without burning the account. Tune per burner reputation.
COOLDOWN_HOURS_ON_SOFT_BLOCK = 2
# If we get a second soft-block within RECENT_BLOCK_WINDOW_HOURS of the
# previous one, IG is actively watching this account. Park it much longer
# instead of bashing IG's rate limiter every 2 hours — repeated short
# cooldowns turn into permanent shadow-bans.
COOLDOWN_HOURS_ON_REPEAT_BLOCK = 24
RECENT_BLOCK_WINDOW_HOURS = 24


def decide_cooldown_hours(
    last_soft_block_at: Optional[datetime],
    now: datetime,
) -> int:
    """Pure: how many hours to cool down given recent soft-block history.

    First block ever (or first in a long while) → short cooldown, the
    incident is probably just a one-off rate trip.

    Block within the last 24h → long cooldown, IG has the account flagged
    and the system needs to back off hard rather than retry every 2h.
    """
    if last_soft_block_at is None:
        return COOLDOWN_HOURS_ON_SOFT_BLOCK
    elapsed = now - last_soft_block_at
    if elapsed < timedelta(hours=RECENT_BLOCK_WINDOW_HOURS):
        return COOLDOWN_HOURS_ON_REPEAT_BLOCK
    return COOLDOWN_HOURS_ON_SOFT_BLOCK


# ────────────────────────────────────────────────────────────────────
# Pick due actions
# ────────────────────────────────────────────────────────────────────

def pick_due_actions(limit: int = BATCH_SIZE) -> list[dict]:
    """
    Actions whose scheduled_for is in the past and still status='scheduled'.
    Skip 'comment' here — those route to Discord, not Playwright.
    """
    sb = get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    rows = (sb.table("warming_actions")
            .select("id,prospect_id,ig_account,action,target_url,scheduled_for")
            .eq("status", "scheduled")
            .in_("action", ["follow", "like_post"])
            .lte("scheduled_for", now)
            .order("scheduled_for", desc=False)
            .limit(limit)
            .execute()).data or []
    return rows


def get_prospect_handle(prospect_id: str) -> Optional[str]:
    sb = get_supabase()
    rows = (sb.table("qualified_prospects")
            .select("handle")
            .eq("id", prospect_id)
            .limit(1)
            .execute()).data or []
    return rows[0]["handle"] if rows else None


def get_account_pk(handle: str) -> Optional[str]:
    """Look up cached pk inside accounts.raw_json to avoid extra profile_loads."""
    sb = get_supabase()
    rows = (sb.table("accounts")
            .select("raw_json")
            .eq("handle", handle)
            .limit(1)
            .execute()).data or []
    if not rows:
        return None
    raw = rows[0].get("raw_json") or {}
    pk = raw.get("pk") or raw.get("id") or raw.get("_pk")
    return str(pk) if pk else None


# ────────────────────────────────────────────────────────────────────
# Account cooldown (IG soft-block protection)
# ────────────────────────────────────────────────────────────────────

def is_account_in_cooldown(ig_account: str) -> bool:
    """
    True if ig_accounts.current_status == 'cooldown' AND cooldown_until is in
    the future. Expired cooldowns return False so the caller can proceed —
    a separate reaper job is expected to flip current_status back to 'active'.
    """
    sb = get_supabase()
    rows = (sb.table("ig_accounts")
            .select("current_status,cooldown_until")
            .eq("handle", ig_account)
            .limit(1)
            .execute()).data or []
    if not rows:
        return False
    row = rows[0]
    if row.get("current_status") != "cooldown":
        return False
    cd_until = row.get("cooldown_until")
    if not cd_until:
        # cooldown set without expiry — treat as indefinite, stay paused
        return True
    try:
        cd_dt = datetime.fromisoformat(cd_until.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        log.warning("m5.executor.cooldown_parse_error", value=cd_until)
        return True
    return cd_dt > datetime.now(timezone.utc)


def set_account_cooldown(
    ig_account: str,
    hours: Optional[int] = None,
    reason: str = "ig_soft_block",
) -> None:
    """
    Flip ig_account into cooldown so no further actions are attempted until
    cooldown_until passes. Updates current_status, cooldown_until,
    last_soft_block_at in one round-trip.

    If `hours` is None, decide_cooldown_hours() picks the duration based on
    recent soft-block history (short for one-offs, long for repeats).
    Explicit `hours` overrides — used by tests and the rare manual call.
    """
    sb = get_supabase()
    now = datetime.now(timezone.utc)

    if hours is None:
        # Read prior soft-block timestamp to decide whether to escalate.
        prev = (sb.table("ig_accounts")
                .select("last_soft_block_at")
                .eq("handle", ig_account)
                .limit(1).execute()).data or []
        prev_ts_raw = prev[0].get("last_soft_block_at") if prev else None
        prev_ts = None
        if prev_ts_raw:
            try:
                prev_ts = datetime.fromisoformat(
                    prev_ts_raw.replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                prev_ts = None
        hours = decide_cooldown_hours(prev_ts, now)

    cd_until = now + timedelta(hours=hours)
    sb.table("ig_accounts").update({
        "current_status": "cooldown",
        "cooldown_until": cd_until.isoformat(),
        "last_soft_block_at": now.isoformat(),
    }).eq("handle", ig_account).execute()
    log.warning("m5.executor.cooldown_set",
                ig_account=ig_account,
                hours=hours,
                reason=reason,
                cooldown_until=cd_until.isoformat())


# ────────────────────────────────────────────────────────────────────
# Status updates
# ────────────────────────────────────────────────────────────────────

def mark_executed(action_id: str) -> None:
    sb = get_supabase()
    sb.table("warming_actions").update({
        "status": "executed",
        "executed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", action_id).execute()


def mark_failed(action_id: str, reason: str) -> None:
    sb = get_supabase()
    sb.table("warming_actions").update({
        "status": "failed",
        "failure_reason": reason[:500],
    }).eq("id", action_id).execute()


def maybe_complete_warmup(prospect_id: str) -> bool:
    """
    If this prospect's follow + both like_posts are all executed,
    transition qualified_prospects.status → warmup_complete with dm_unblock_at.

    Returns True if a transition happened.
    """
    sb = get_supabase()
    rows = (sb.table("warming_actions")
            .select("action,status")
            .eq("prospect_id", prospect_id)
            .in_("action", ["follow", "like_post"])
            .execute()).data or []
    if not rows:
        return False

    all_done = all(r["status"] == "executed" for r in rows)
    if not all_done:
        return False

    dm_unblock = datetime.now(timezone.utc) + timedelta(hours=24)
    sb.table("qualified_prospects").update({
        "status": "warmup_complete",
        "dm_unblock_at": dm_unblock.isoformat(),
    }).eq("id", prospect_id).execute()
    log.info("m5.warmup.complete",
             prospect_id=prospect_id,
             dm_unblock_at=dm_unblock.isoformat())
    return True


# ────────────────────────────────────────────────────────────────────
# Per-action handlers
# ────────────────────────────────────────────────────────────────────

async def execute_follow(page, prospect_handle: str) -> dict:
    """
    Resolve handle → pk (cached or via 1 profile_load), then POST follow.
    Navigates to instagram.com first so document.cookie is accessible.
    """
    # Land on instagram.com so the page has IG cookies available
    await page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
    await human_sleep(2.0, 5.0)

    pk = get_account_pk(prospect_handle)
    if not pk:
        pk = await resolve_user_pk(page, prospect_handle)
    if not pk:
        raise IGAPIError(f"could not resolve pk for @{prospect_handle}")
    return await follow_user(page, pk)


def count_executed_likes_for(prospect_id: str) -> int:
    """How many like_post warming actions have already executed for this prospect."""
    sb = get_supabase()
    rows = (sb.table("warming_actions")
            .select("id", count="exact")
            .eq("prospect_id", prospect_id)
            .eq("action", "like_post")
            .eq("status", "executed")
            .execute())
    return rows.count or 0


def pick_media_index(prior_executed_likes: int, available: int) -> int:
    """
    Pure: given how many like_posts already executed and how many recent
    media we just fetched, return which media index to like NOW.

    Strategy: round-robin through the most recent N posts. Like #1 → media[0],
    like #2 → media[1], etc. If we've already gone around once, wrap.

    The original code always returned 0 — second like was a silent no-op
    against an already-liked post. With wrap-around, the second like
    actually lands on a different post the prospect can see.
    """
    if available <= 0:
        raise ValueError("no media available")
    return prior_executed_likes % available


async def execute_like_post(page, prospect_handle: str, prospect_id: str) -> dict:
    """
    Resolve pk → fetch 3 most recent media → like one we haven't liked yet.

    Picks the media index by counting prior executed like_posts for this
    prospect, so like #1 hits media[0], like #2 hits media[1], etc. Wraps
    if there are more likes than recent posts (shouldn't happen in V1 —
    planner only schedules 2 likes per prospect — but defensive).
    """
    await page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
    await human_sleep(2.0, 5.0)

    pk = get_account_pk(prospect_handle)
    if not pk:
        pk = await resolve_user_pk(page, prospect_handle)
    if not pk:
        raise IGAPIError(f"could not resolve pk for @{prospect_handle}")

    media_ids = await fetch_recent_media_ids(page, pk, count=3)
    if not media_ids:
        raise IGAPIError(f"no recent media found for @{prospect_handle}")

    prior = count_executed_likes_for(prospect_id)
    idx = pick_media_index(prior_executed_likes=prior, available=len(media_ids))
    media_to_like = media_ids[idx]
    log.info(
        "m5.executor.like_media_picked",
        handle=prospect_handle,
        index=idx,
        prior_executed=prior,
        available=len(media_ids),
    )
    return await like_media(page, media_to_like)


# ────────────────────────────────────────────────────────────────────
# Story actions — Mason's "swipe + heart" engagement signal
# ────────────────────────────────────────────────────────────────────


class NoActiveStories(Exception):
    """Raised when the prospect has no live stories at execution time.

    Stories expire after 24h, so this is a routine outcome — not a bug.
    The executor catches this and marks the action `cancelled` (rather
    than `failed`), preserving the prospect's warm-up momentum.
    """


def _pick_story(stories: list[dict]) -> dict:
    """Choose which story to engage with — pick a random one out of the
    available active stories so behaviour doesn't look mechanical."""
    return random.choice(stories)


async def execute_view_story(page, prospect_handle: str) -> dict:
    """
    Mason's "occasional swiping interactions" — open a random active story
    and mark it as viewed. Raises NoActiveStories if there's nothing to view.
    """
    await page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
    await human_sleep(2.0, 5.0)

    pk = get_account_pk(prospect_handle)
    if not pk:
        pk = await resolve_user_pk(page, prospect_handle)
    if not pk:
        raise IGAPIError(f"could not resolve pk for @{prospect_handle}")

    stories = await fetch_active_stories(page, pk)
    if not stories:
        raise NoActiveStories(f"@{prospect_handle} has no active stories")

    story = _pick_story(stories)
    log.info(
        "m5.executor.story_view_picked",
        handle=prospect_handle,
        media_id=story["id"],
        available=len(stories),
    )
    return await view_story(page, story)


async def execute_like_story(page, prospect_handle: str) -> dict:
    """
    Mason's literal spec: "automatically 'heart' story updates ..."

    Picks one active story at random and likes it. NoActiveStories if the
    user has nothing live right now.
    """
    await page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
    await human_sleep(2.0, 5.0)

    pk = get_account_pk(prospect_handle)
    if not pk:
        pk = await resolve_user_pk(page, prospect_handle)
    if not pk:
        raise IGAPIError(f"could not resolve pk for @{prospect_handle}")

    stories = await fetch_active_stories(page, pk)
    if not stories:
        raise NoActiveStories(f"@{prospect_handle} has no active stories")

    story = _pick_story(stories)
    log.info(
        "m5.executor.story_like_picked",
        handle=prospect_handle,
        media_id=story["id"],
        available=len(stories),
    )
    return await like_story(page, story)


# ────────────────────────────────────────────────────────────────────
# Main loop
# ────────────────────────────────────────────────────────────────────

async def execute_one(action_row: dict, ig_account: str) -> bool:
    """
    Execute one warming_actions row. Update status accordingly.

    Returns:
        True if the action triggered an IG soft-block — caller should abort
        the rest of the batch and let cooldown_until pass before retrying.
        False on success or non-cooldown failures (caller continues batch).
    """
    action_id = action_row["id"]
    action = action_row["action"]
    prospect_id = action_row["prospect_id"]

    prospect_handle = get_prospect_handle(prospect_id)
    if not prospect_handle:
        mark_failed(action_id, "prospect not found")
        return False

    # Map action → rate-limiter resource
    resource_map = {"follow": "follows", "like_post": "likes"}
    resource = resource_map.get(action)
    if not resource:
        mark_failed(action_id, f"unknown action: {action}")
        return False

    # Check rate limit (will raise inside if exceeded)
    try:
        check_and_consume(ig_account, resource, amount=1)
    except RateLimitExceeded as e:
        log.warning("m5.executor.rate_limited",
                    action=action, handle=prospect_handle, err=str(e))
        # Don't fail — leave scheduled so it retries later when budget refreshes
        return False
    except AccountUnavailable as e:
        log.warning("m5.executor.account_unavailable",
                    action=action, handle=prospect_handle, err=str(e))
        return False

    log.info("m5.executor.start",
             action=action, handle=prospect_handle, action_id=action_id)

    try:
        async with ig_session(ig_account) as page:
            if action == "follow":
                await execute_follow(page, prospect_handle)
            elif action == "like_post":
                await execute_like_post(page, prospect_handle, prospect_id)
            elif action == "story_view":
                await execute_view_story(page, prospect_handle)
            elif action == "story_like":
                await execute_like_story(page, prospect_handle)
            else:
                raise IGAPIError(f"unhandled action: {action}")

        mark_executed(action_id)
        maybe_complete_warmup(prospect_id)
        log.info("m5.executor.done", action=action, handle=prospect_handle)
        return False
    except NoActiveStories as e:
        # Soft skip — the prospect just doesn't have live stories right now.
        # Don't mark `failed` (which suggests a bug); mark `cancelled` with
        # a clear reason so analytics can tell intent from incident.
        sb = get_supabase()
        sb.table("warming_actions").update({
            "status": "cancelled",
            "failure_reason": "no_active_stories",
        }).eq("id", action_id).execute()
        log.info(
            "m5.executor.story_skip_no_active",
            action=action, handle=prospect_handle, err=str(e),
        )
        # Still attempt to complete warm-up — a missed story shouldn't block.
        maybe_complete_warmup(prospect_id)
        return False
    except IGRateLimited as e:
        # IG soft-block: flip the account into cooldown so the rest of this
        # batch (and the next scheduler tick) doesn't keep hammering the same
        # endpoint and digging the reputation hole deeper. Action stays
        # 'scheduled' so it retries after cooldown_until passes.
        log.warning("m5.executor.rate_limited_by_ig",
                    action=action, handle=prospect_handle, err=str(e))
        set_account_cooldown(
            ig_account,
            reason=f"ig_soft_block on {action} for @{prospect_handle}",
        )
        return True
    except IGNotFound as e:
        mark_failed(action_id, f"not_found: {e}")
        log.warning("m5.executor.not_found",
                    action=action, handle=prospect_handle, err=str(e))
        return False
    except IGAPIError as e:
        mark_failed(action_id, f"ig_error: {e}")
        log.error("m5.executor.failed",
                  action=action, handle=prospect_handle, err=str(e))
        return False
    except Exception as e:
        mark_failed(action_id, f"unexpected: {e}")
        log.exception("m5.executor.unexpected",
                      action=action, handle=prospect_handle)
        return False


async def execute_batch(ig_account: str = "ignorethisdump2") -> int:
    """
    Pick due actions, run them sequentially with jitter between.

    Skip the whole batch if the account is in cooldown. Abort mid-batch if
    any action triggers a soft-block so the cooldown actually buys us time
    instead of getting eaten by the next item in the queue.
    """
    import random
    if is_account_in_cooldown(ig_account):
        log.info("m5.executor.account_in_cooldown", ig_account=ig_account)
        return 0
    actions = pick_due_actions()
    if not actions:
        return 0
    log.info("m5.executor.batch", size=len(actions))
    executed = 0
    for i, action_row in enumerate(actions):
        cooldown_triggered = await execute_one(action_row, ig_account)
        executed += 1
        if cooldown_triggered:
            log.warning("m5.executor.batch_aborted",
                        ig_account=ig_account,
                        executed=executed,
                        skipped=len(actions) - executed,
                        reason="ig_soft_block")
            break
        if i < len(actions) - 1:
            delay = random.randint(BETWEEN_ACTIONS_MIN_S, BETWEEN_ACTIONS_MAX_S)
            log.info("m5.executor.sleep", seconds=delay)
            await asyncio.sleep(delay)
    return executed
