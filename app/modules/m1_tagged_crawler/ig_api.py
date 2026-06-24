"""
Instagram web API client.

Uses IG's public web frontend endpoints (the same ones the browser uses).
NOT the Graph API — that requires app review and doesn't expose tagged photos
or follower graphs anyway.

Endpoints used:
- /api/v1/users/web_profile_info/?username={handle}  — profile metadata
- /api/v1/feed/user/{user_id}/tagged/?count=12       — tagged photos tab
- /api/v1/tags/web_info/?tag_name={tag}              — hashtag metadata

These endpoints require:
- A valid session cookie (csrftoken + sessionid)
- The x-ig-app-id header (936619743392459 = web app)
- A realistic user-agent

All calls go through the Playwright context to stay sticky with cookies + proxy.
"""
from __future__ import annotations
import json
from typing import Optional
from playwright.async_api import Page
from app.core.logging import get_logger

log = get_logger(__name__)

IG_APP_ID = "936619743392459"
IG_API_BASE = "https://www.instagram.com/api/v1"


class IGAPIError(Exception):
    pass


class IGRateLimited(IGAPIError):
    """Hit a 429 or login-required redirect."""


class IGNotFound(IGAPIError):
    """User/hashtag doesn't exist."""


async def _api_get(page: Page, path: str, params: Optional[dict] = None) -> dict:
    """
    Make an authenticated API call from within the Playwright page context.

    We use page.evaluate() with fetch() so the request inherits the page's
    cookies and proxy automatically — no separate httpx session needed.
    """
    qs = ""
    if params:
        from urllib.parse import urlencode
        qs = "?" + urlencode(params)

    url = f"{IG_API_BASE}{path}{qs}"

    js = f"""
    async () => {{
        const csrf = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || '';
        const r = await fetch({json.dumps(url)}, {{
            method: 'GET',
            credentials: 'include',
            headers: {{
                'x-ig-app-id': {json.dumps(IG_APP_ID)},
                'x-asbd-id': '198387',
                'x-requested-with': 'XMLHttpRequest',
                'x-csrftoken': csrf,
                'accept': '*/*',
            }}
        }});
        const text = await r.text();
        return {{status: r.status, body: text, url: r.url}};
    }}
    """

    result = await page.evaluate(js)
    status = result["status"]

    if status == 429 or "login" in result["url"]:
        raise IGRateLimited(f"Rate limited or session expired on {path}")
    if status == 404:
        raise IGNotFound(f"Not found: {path}")
    if status >= 400:
        raise IGAPIError(f"IG API {status} on {path}: {result['body'][:200]}")

    try:
        return json.loads(result["body"])
    except json.JSONDecodeError as e:
        raise IGAPIError(f"Bad JSON from {path}: {e}") from e


async def get_profile(page: Page, handle: str) -> dict:
    """
    Fetch full profile metadata for a handle.

    Returns the IG `user` object containing: pk, username, full_name,
    biography, follower_count, following_count, media_count, is_business,
    external_url, profile_pic_url, etc.
    """
    handle = handle.lstrip("@").lower()
    data = await _api_get(page, "/users/web_profile_info/", {"username": handle})
    user = data.get("data", {}).get("user")
    if not user:
        raise IGNotFound(f"No user data for @{handle}")
    return user


async def get_tagged_posts(page: Page, user_pk: str, max_posts: int = 50) -> list[dict]:
    """
    Fetch the 'tagged in' feed for a user.

    Returns list of post objects. Each post has 'usertags' field listing
    all users tagged in the photo — that's the rabbit hole entry point.
    """
    posts = []
    next_max_id = None

    while len(posts) < max_posts:
        params = {"count": min(12, max_posts - len(posts))}
        if next_max_id:
            params["max_id"] = next_max_id

        try:
            data = await _api_get(page, f"/usertags/{user_pk}/feed/", params)
        except IGAPIError as e:
            log.warning("ig.tagged.fetch_failed", user_pk=user_pk, err=str(e))
            break

        items = data.get("items", [])
        if not items:
            break
        posts.extend(items)

        if not data.get("more_available"):
            break
        next_max_id = data.get("next_max_id")
        if not next_max_id:
            break

    return posts[:max_posts]


def extract_tagged_users(post: dict) -> list[dict]:
    """
    Pull all tagged user objects from a post.

    Returns: [{'pk': '123', 'username': 'jane', 'full_name': 'Jane Doe'}, ...]
    """
    tagged = []
    usertags = post.get("usertags", {}).get("in", [])
    for tag in usertags:
        u = tag.get("user", {})
        if u.get("username"):
            tagged.append({
                "pk": str(u.get("pk", "")),
                "username": u["username"],
                "full_name": u.get("full_name", ""),
                "is_verified": u.get("is_verified", False),
            })
    return tagged


def extract_post_url(post: dict) -> str:
    code = post.get("code") or post.get("pk")
    return f"https://www.instagram.com/p/{code}/"


def normalize_profile(user: dict) -> dict:
    """Convert raw IG user object to our `accounts` table shape."""
    return {
        "handle": user["username"].lower(),
        "full_name": user.get("full_name") or None,
        "bio": user.get("biography") or None,
        "follower_count": user.get("edge_followed_by", {}).get("count")
                          or user.get("follower_count"),
        "following_count": user.get("edge_follow", {}).get("count")
                           or user.get("following_count"),
        "post_count": user.get("edge_owner_to_timeline_media", {}).get("count")
                      or user.get("media_count"),
        "is_business": user.get("is_business") or user.get("is_business_account"),
        "external_url": user.get("external_url") or None,
        "profile_pic_url": user.get("profile_pic_url_hd") or user.get("profile_pic_url"),
        "raw_json": user,
        "_pk": str(user.get("pk") or user.get("id") or ""),  # internal, not stored
    }
