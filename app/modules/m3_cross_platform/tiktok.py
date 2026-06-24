"""
TikTok lookup — given an IG handle (and optionally full_name), check if the
prospect has an active TikTok presence.

Strategy: TikTok doesn't expose a stable public API, so we use their web
search endpoint via a Playwright session. Returns:
- whether a matching account was found
- their username, follower count, last post recency

Mason's tactic: discovering on TikTok → opener on IG ("Saw you on TikTok,
but hitting you on IG"). So we capture this as cross_platform_discovery_source.
"""
from __future__ import annotations
import re
from typing import Optional
from playwright.async_api import async_playwright
from app.core.logging import get_logger

log = get_logger(__name__)

TIKTOK_USERNAME_RE = re.compile(r"^[a-z0-9._]{2,24}$")


def _candidate_handles(ig_handle: str, full_name: Optional[str]) -> list[str]:
    """Generate plausible TikTok handles to test directly via /@username."""
    candidates = []
    handle = ig_handle.lower().lstrip("@")
    candidates.append(handle)

    # Common variations
    candidates.append(handle.replace(".", ""))
    candidates.append(handle.replace("_", ""))

    if full_name:
        # firstname_lastname, firstnamelastname, etc.
        parts = re.findall(r"[a-zA-Z]+", full_name.lower())
        if len(parts) >= 2:
            candidates.append(f"{parts[0]}{parts[1]}")
            candidates.append(f"{parts[0]}.{parts[1]}")
            candidates.append(f"{parts[0]}_{parts[1]}")
        if len(parts) >= 1:
            candidates.append(parts[0])

    # Dedupe + filter
    seen = set()
    out = []
    for c in candidates:
        if c not in seen and TIKTOK_USERNAME_RE.match(c):
            seen.add(c)
            out.append(c)
    return out[:5]


async def find_tiktok(ig_handle: str, full_name: Optional[str] = None) -> Optional[dict]:
    """
    Try to find a TikTok account matching this IG profile.

    Returns None if not found, else:
    {
        'platform_handle': 'username',
        'platform_url': 'https://tiktok.com/@username',
        'follower_count': int,
        'has_active_content': bool,
    }
    """
    candidates = _candidate_handles(ig_handle, full_name)
    if not candidates:
        return None

    async with async_playwright() as pw:
        # No proxy on cross-platform — TikTok rate limits less aggressively and
        # we want to keep IG proxies dedicated to IG work
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148"
            ),
            viewport={"width": 390, "height": 844},
            is_mobile=True,
        )
        page = await ctx.new_page()

        try:
            for candidate in candidates:
                url = f"https://www.tiktok.com/@{candidate}"
                try:
                    resp = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                except Exception as e:
                    log.debug("m3.tiktok.nav_failed", candidate=candidate, err=str(e))
                    continue

                if not resp or resp.status >= 400:
                    continue

                # TikTok renders user data in __UNIVERSAL_DATA_FOR_REHYDRATION__ script
                try:
                    data = await page.evaluate(
                        "() => { const el = document.getElementById("
                        "'__UNIVERSAL_DATA_FOR_REHYDRATION__'); "
                        "return el ? el.textContent : null; }"
                    )
                except Exception:
                    data = None

                if not data:
                    continue

                import json
                try:
                    blob = json.loads(data)
                except json.JSONDecodeError:
                    continue

                user_info = (blob.get("__DEFAULT_SCOPE__", {})
                             .get("webapp.user-detail", {})
                             .get("userInfo", {}))
                if not user_info:
                    continue

                stats = user_info.get("stats", {})
                user = user_info.get("user", {})

                if not user.get("uniqueId"):
                    continue

                # Loose match check: nickname or uniqueId should overlap with our hints
                nickname = (user.get("nickname") or "").lower()
                unique = user["uniqueId"].lower()
                signal_overlap = (
                    unique == ig_handle.lower() or
                    (full_name and any(p in nickname for p in full_name.lower().split()))
                )
                if not signal_overlap and len(candidates) > 1:
                    # Be cautious about false positives unless we're confident
                    continue

                return {
                    "platform_handle": unique,
                    "platform_url": f"https://www.tiktok.com/@{unique}",
                    "follower_count": stats.get("followerCount", 0),
                    "has_active_content": stats.get("videoCount", 0) > 5,
                }

            return None
        finally:
            await browser.close()
