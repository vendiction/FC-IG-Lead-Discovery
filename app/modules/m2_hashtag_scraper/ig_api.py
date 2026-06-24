"""
M2 Hashtag Scraper — IG hashtag API client.

Endpoints:
- /api/v1/tags/web_info/?tag_name={tag}   — tag metadata (post count, etc.)
- /api/v1/tags/{tag}/sections/             — top + recent posts

Returns post authors which become candidates in the `accounts` table.
"""
from __future__ import annotations
import json
from typing import Literal
from playwright.async_api import Page
from app.core.logging import get_logger
from app.modules.m1_tagged_crawler.ig_api import (
    _api_get, IGAPIError, IGRateLimited, IGNotFound, IG_API_BASE, IG_APP_ID,
)

log = get_logger(__name__)

Section = Literal["top", "recent"]


async def get_hashtag_info(page: Page, tag: str) -> dict:
    """Fetch tag metadata. Confirms tag exists and gets post count."""
    tag = tag.lstrip("#").lower()
    data = await _api_get(page, "/tags/web_info/", {"tag_name": tag})
    info = data.get("data", {}).get("recent") or data.get("data", {})
    return info if info else {}


async def get_hashtag_posts(page: Page, tag: str, section: Section = "recent",
                            max_posts: int = 100) -> list[dict]:
    """
    Fetch top or recent posts for a hashtag.

    Uses the sections endpoint which returns paginated post grids.
    Each post in the result has 'user' field with the author.
    """
    tag = tag.lstrip("#").lower()
    posts: list[dict] = []
    next_max_id: str | None = None
    next_page: int = 0

    while len(posts) < max_posts:
        body_payload = {
            "include_persistent": "0",
            "max_id": next_max_id or "",
            "page": str(next_page),
            "surface": "grid",
            "tab": section,
        }

        url = f"{IG_API_BASE}/tags/{tag}/sections/"
        form = "&".join(f"{k}={v}" for k, v in body_payload.items())

        js = f"""
        async () => {{
            const csrf = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || '';
            const r = await fetch({json.dumps(url)}, {{
                method: 'POST',
                credentials: 'include',
                headers: {{
                    'x-ig-app-id': {json.dumps(IG_APP_ID)},
                    'x-csrftoken': csrf,
                    'content-type': 'application/x-www-form-urlencoded',
                    'accept': '*/*',
                }},
                body: {json.dumps(form)}
            }});
            const text = await r.text();
            return {{status: r.status, body: text, url: r.url}};
        }}
        """
        result = await page.evaluate(js)

        if result["status"] == 429 or "login" in result["url"]:
            raise IGRateLimited(f"Rate limited on hashtag {tag}")
        if result["status"] == 404:
            raise IGNotFound(f"Hashtag #{tag} not found")
        if result["status"] >= 400:
            raise IGAPIError(f"Hashtag API {result['status']}: {result['body'][:200]}")

        try:
            data = json.loads(result["body"])
        except json.JSONDecodeError:
            break

        sections = data.get("sections", [])
        any_new = False
        for section_obj in sections:
            media = (section_obj.get("layout_content", {})
                     .get("medias")
                     or section_obj.get("layout_content", {}).get("fill_items", []))
            for m in media:
                post = m.get("media") if isinstance(m, dict) and "media" in m else m
                if post:
                    posts.append(post)
                    any_new = True

        if not data.get("more_available") or not any_new:
            break
        next_max_id = data.get("next_max_id")
        next_page = data.get("next_page", next_page + 1)

    return posts[:max_posts]


def extract_post_author(post: dict) -> dict | None:
    """Get the author dict from a hashtag post."""
    user = post.get("user") or post.get("owner") or {}
    if not user.get("username"):
        return None
    return {
        "pk": str(user.get("pk", "")),
        "username": user["username"].lower(),
        "full_name": user.get("full_name", ""),
        "is_verified": user.get("is_verified", False),
    }
