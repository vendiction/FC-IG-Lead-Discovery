"""
M5 IG Actions.

Implements the action primitives used during pre-DM warm-up:
- follow_user(page, target_pk) → POST /api/v1/friendships/create/{user_pk}/
- like_media(page, media_id) → POST /api/v1/media/{media_id}/like/
- fetch_recent_media_ids(page, user_pk, count) → GET feed/user for picking like targets

All actions:
- Use the same authenticated Playwright page that M1 uses
- Include the CSRF token from the page's cookie jar
- Return parsed JSON or raise IGAPIError / IGRateLimited
- Log structured events
"""
from __future__ import annotations
import json
from typing import Optional
from playwright.async_api import Page
from app.core.logging import get_logger
from app.modules.m1_tagged_crawler.ig_api import (
    IGAPIError, IGRateLimited, IGNotFound, IG_API_BASE, IG_APP_ID, _api_get,
)

log = get_logger(__name__)


# ────────────────────────────────────────────────────────────────────
# Internal: authenticated POST with CSRF
# ────────────────────────────────────────────────────────────────────

async def _api_post(page: Page, path: str, form: Optional[dict] = None) -> dict:
    """
    POST to an IG API endpoint from inside the Playwright page context.
    Reads csrftoken from document.cookie and sends as x-csrftoken header.
    """
    url = f"{IG_API_BASE}{path}"
    body = ""
    if form:
        from urllib.parse import urlencode
        body = urlencode(form)

    js = f"""
    async () => {{
        const csrf = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || '';
        const r = await fetch({json.dumps(url)}, {{
            method: 'POST',
            credentials: 'include',
            headers: {{
                'x-ig-app-id': {json.dumps(IG_APP_ID)},
                'x-csrftoken': csrf,
                'x-asbd-id': '198387',
                'x-requested-with': 'XMLHttpRequest',
                'content-type': 'application/x-www-form-urlencoded',
                'accept': '*/*',
            }},
            body: {json.dumps(body)}
        }});
        const text = await r.text();
        return {{status: r.status, body: text, url: r.url}};
    }}
    """
    result = await page.evaluate(js)
    status = result["status"]

    if status == 429:
        raise IGRateLimited(f"429 rate limit on {path}; body: {result['body'][:300]}")
    if "login" in result["url"]:
        raise IGRateLimited(
            f"redirected to login on {path}; status={status} final_url={result['url']}; "
            f"body: {result['body'][:300]}"
        )
    if status == 404:
        raise IGNotFound(f"Target not found at {path}: {result['body'][:300]}")
    if status == 403:
        # Common cause: action limit / suspicious behavior detection
        raise IGAPIError(f"IG 403 on {path} — possible action block: {result['body'][:300]}")
    if status >= 400:
        raise IGAPIError(f"IG {status} on {path}: {result['body'][:300]}")

    try:
        return json.loads(result["body"])
    except json.JSONDecodeError:
        raise IGAPIError(f"Non-JSON response from {path}: {result['body'][:200]}")


# ────────────────────────────────────────────────────────────────────
# Action: follow a user
# ────────────────────────────────────────────────────────────────────

async def follow_user(page: Page, target_pk: str) -> dict:
    """
    Follow a user by their user_pk.

    Returns IG's response which includes friendship_status fields.
    Raises if the action is blocked.
    """
    log.info("m5.action.follow.start", target_pk=target_pk)
    try:
        result = await _api_post(page, f"/friendships/create/{target_pk}/")
    except IGAPIError:
        log.exception("m5.action.follow.failed", target_pk=target_pk)
        raise

    status = result.get("status") or result.get("friendship_status", {}).get("following")
    log.info("m5.action.follow.done", target_pk=target_pk, ig_status=status)
    return result


# ────────────────────────────────────────────────────────────────────
# Action: like a media post
# ────────────────────────────────────────────────────────────────────

async def like_media(page: Page, media_id: str) -> dict:
    """
    Like a post by its media_id.

    media_id format is "<pk>_<owner_pk>" (e.g. "3401234567890123456_25025320").
    Uses the modern IG mobile/web shared endpoint /media/{id}/like/.
    """
    log.info("m5.action.like.start", media_id=media_id)
    try:
        result = await _api_post(
            page,
            f"/media/{media_id}/like/",
            form={
                "media_id": media_id,
                "container_module": "feed_timeline",
            },
        )
    except IGAPIError:
        log.exception("m5.action.like.failed", media_id=media_id)
        raise

    log.info("m5.action.like.done", media_id=media_id, ig_status=result.get("status"))
    return result


# ────────────────────────────────────────────────────────────────────
# Helper: fetch recent media for a user (to pick a like target)
# ────────────────────────────────────────────────────────────────────

async def fetch_recent_media_ids(page: Page, user_pk: str, count: int = 3) -> list[str]:
    """
    Get the most recent N media_ids posted by a user.
    Used to pick what to like during warm-up.
    """
    log.info("m5.fetch_recent_media.start", user_pk=user_pk, count=count)
    try:
        data = await _api_get(page, f"/feed/user/{user_pk}/", {"count": str(count)})
    except IGAPIError:
        log.exception("m5.fetch_recent_media.failed", user_pk=user_pk)
        raise

    items = data.get("items", []) or []
    media_ids = []
    for it in items[:count]:
        mid = it.get("id") or it.get("pk")
        if mid:
            media_ids.append(str(mid))

    log.info("m5.fetch_recent_media.done", user_pk=user_pk, found=len(media_ids))
    return media_ids


# ────────────────────────────────────────────────────────────────────
# Helper: get user_pk from handle (resolves handle → pk for the action calls)
# ────────────────────────────────────────────────────────────────────

async def resolve_user_pk(page: Page, handle: str) -> Optional[str]:
    """
    Look up a user's pk by handle. Costs 1 profile_loads call from the rate limiter.
    """
    log.info("m5.resolve_pk.start", handle=handle)
    try:
        data = await _api_get(page, "/users/web_profile_info/", {"username": handle})
    except IGNotFound:
        log.warning("m5.resolve_pk.not_found", handle=handle)
        return None

    user = (data.get("data") or {}).get("user") or {}
    pk = user.get("pk") or user.get("id")
    log.info("m5.resolve_pk.done", handle=handle, pk=pk)
    return str(pk) if pk else None


# ────────────────────────────────────────────────────────────────────
# Action: fetch a user's active stories
# ────────────────────────────────────────────────────────────────────

async def fetch_active_stories(page: Page, user_pk: str) -> list[dict]:
    """
    Fetch the user's currently active story reel (24h window).

    Returns a list of story-item dicts with keys we care about:
      - id           — the media id, format "<pk>_<owner_pk>"
      - pk           — the numeric pk alone
      - taken_at     — unix timestamp; required by /media/seen/
      - owner_pk     — owner user_pk

    Returns [] if the user has no active stories (very common — most
    users don't post stories every day). Callers should treat an empty
    result as a soft skip, not a failure.
    """
    log.info("m5.fetch_stories.start", user_pk=user_pk)
    try:
        data = await _api_get(
            page, "/feed/reels_media/", {"reel_ids[]": user_pk}
        )
    except IGAPIError:
        log.exception("m5.fetch_stories.failed", user_pk=user_pk)
        raise

    # IG returns {"reels": {"<pk>": {"items": [...]}}, "reels_media": [...]}.
    # Shape varies across web vs mobile API versions — handle both.
    reels = data.get("reels") or {}
    reel = reels.get(str(user_pk)) or {}
    items_raw = reel.get("items") or data.get("reels_media", [])
    # If reels_media is a list-of-reels, find the one for our user.
    if isinstance(items_raw, list) and items_raw and "items" in items_raw[0]:
        items_raw = next(
            (r["items"] for r in items_raw if str(r.get("id")) == str(user_pk)),
            [],
        )

    stories = []
    for it in items_raw:
        sid = it.get("id") or it.get("pk")
        if not sid:
            continue
        # Normalize media_id to "<pk>_<owner_pk>" form if it's just numeric pk
        sid_str = str(sid)
        if "_" not in sid_str:
            sid_str = f"{sid_str}_{user_pk}"
        stories.append({
            "id": sid_str,
            "pk": str(it.get("pk") or sid_str.split("_")[0]),
            "taken_at": it.get("taken_at") or 0,
            "owner_pk": str(user_pk),
        })

    log.info("m5.fetch_stories.done", user_pk=user_pk, count=len(stories))
    return stories


# ────────────────────────────────────────────────────────────────────
# Action: mark a story as viewed (Mason's "swipe through" interaction)
# ────────────────────────────────────────────────────────────────────

async def view_story(page: Page, story: dict) -> dict:
    """
    Mark one story as viewed. Implements Mason's spec line "perform
    occasional swiping interactions to create a history of engagement"
    — at the API level, a swipe-through IS a view, which IG records and
    surfaces in the owner's "story viewers" list.

    `story` is a dict from fetch_active_stories(). Required keys:
    `id` (media_id "<pk>_<owner>") and `taken_at` (unix timestamp).

    IG batches viewed-stories via /media/seen/ with form `reels` mapping
    media_id → array of "<taken_at>_<seen_at>" strings. We POST one
    story per call to keep behaviour simple and aligned with M5's
    one-action-at-a-time pacing.
    """
    import time
    media_id = story["id"]
    taken_at = story["taken_at"]
    seen_at = int(time.time())

    log.info("m5.action.story_view.start", media_id=media_id)
    try:
        result = await _api_post(
            page,
            "/media/seen/",
            form={
                # IG expects reels[media_id]=["<taken_at>_<seen_at>"]
                f"reels[{media_id}][]": f"{taken_at}_{seen_at}",
                "container_module": "feed_timeline",
                "live_vods": "",
                "nuxes_skipped": "",
                "nuxes": "",
            },
        )
    except IGAPIError:
        log.exception("m5.action.story_view.failed", media_id=media_id)
        raise

    log.info("m5.action.story_view.done",
             media_id=media_id, ig_status=result.get("status"))
    return result


# ────────────────────────────────────────────────────────────────────
# Action: "heart" a story (Mason's literal spec word)
# ────────────────────────────────────────────────────────────────────

async def like_story(page: Page, story: dict) -> dict:
    """
    "Heart" a story update. Mason's spec: "automatically 'heart' story
    updates ... to create a history of engagement."

    `story` is a dict from fetch_active_stories(). Required keys: `id`
    ("<pk>_<owner_pk>") and `owner_pk`.

    IG's story-like endpoint takes the media_id and tagged_user_id (owner).
    Returns IG's status response. Raises on rate limit / API error like
    the rest of M5's actions.
    """
    media_id = story["id"]
    owner_pk = story["owner_pk"]

    log.info("m5.action.story_like.start",
             media_id=media_id, owner_pk=owner_pk)
    try:
        result = await _api_post(
            page,
            f"/media/{media_id}/{owner_pk}/story_like/",
            form={
                "container_module": "reel_feed_timeline",
                "tap_state_owner": owner_pk,
                "tap_state": "1",
            },
        )
    except IGAPIError:
        log.exception("m5.action.story_like.failed",
                      media_id=media_id, owner_pk=owner_pk)
        raise

    log.info("m5.action.story_like.done",
             media_id=media_id, ig_status=result.get("status"))
    return result
