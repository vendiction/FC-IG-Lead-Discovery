"""
YouTube lookup — find prospect's channel via YouTube Data API v3.

Uses the search.list endpoint to find channels matching the prospect's name
or IG handle. Returns subscriber count, video count, and recency.

Mason cares about: does prospect have a YouTube channel (signals active brand)?
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional
import httpx
from app.core.logging import get_logger
from app.core.settings import get_settings

log = get_logger(__name__)

YT_API = "https://www.googleapis.com/youtube/v3"


async def find_youtube(ig_handle: str, full_name: Optional[str] = None) -> Optional[dict]:
    """
    Search YouTube for a channel matching the prospect.

    Returns None if no key configured / no match, else:
    {
        'platform_handle': '@channelhandle' or channelId,
        'platform_url': 'https://youtube.com/...',
        'follower_count': int (subscribers),
        'has_active_content': bool,
        'last_post_at': iso timestamp or None,
    }
    """
    settings = get_settings()
    if not settings.youtube_api_key:
        log.debug("m3.youtube.no_api_key")
        return None

    query = full_name or ig_handle
    query = query[:100]

    async with httpx.AsyncClient(timeout=15.0) as client:
        # 1. Search for channels
        r = await client.get(f"{YT_API}/search", params={
            "part": "snippet",
            "type": "channel",
            "q": query,
            "maxResults": 5,
            "key": settings.youtube_api_key,
        })
        if r.status_code != 200:
            log.warning("m3.youtube.search_failed",
                        status=r.status_code, body=r.text[:200])
            return None
        items = r.json().get("items", [])
        if not items:
            return None

        # 2. Loose match heuristic
        candidates = []
        handle_l = ig_handle.lower()
        name_l = (full_name or "").lower()
        for it in items:
            sn = it.get("snippet", {})
            title = (sn.get("channelTitle") or sn.get("title") or "").lower()
            desc = (sn.get("description") or "").lower()
            channel_id = (sn.get("channelId")
                          or it.get("id", {}).get("channelId"))
            if not channel_id:
                continue

            score = 0
            if handle_l in title or handle_l in desc:
                score += 3
            if name_l and name_l in title:
                score += 2
            if name_l and any(p in title for p in name_l.split() if len(p) > 2):
                score += 1
            if score > 0:
                candidates.append((score, channel_id, sn))

        if not candidates:
            return None

        candidates.sort(reverse=True)
        _, channel_id, snippet = candidates[0]

        # 3. Get channel stats
        r2 = await client.get(f"{YT_API}/channels", params={
            "part": "snippet,statistics,contentDetails",
            "id": channel_id,
            "key": settings.youtube_api_key,
        })
        if r2.status_code != 200:
            return None
        cdata = r2.json().get("items", [])
        if not cdata:
            return None
        ch = cdata[0]
        stats = ch.get("statistics", {})
        ch_snippet = ch.get("snippet", {})

        # 4. Check recency via latest video
        last_post = None
        uploads_pid = (ch.get("contentDetails", {})
                       .get("relatedPlaylists", {}).get("uploads"))
        if uploads_pid:
            r3 = await client.get(f"{YT_API}/playlistItems", params={
                "part": "snippet",
                "playlistId": uploads_pid,
                "maxResults": 1,
                "key": settings.youtube_api_key,
            })
            if r3.status_code == 200:
                pl_items = r3.json().get("items", [])
                if pl_items:
                    last_post = (pl_items[0].get("snippet", {})
                                 .get("publishedAt"))

        # Active if posted in last 60 days
        has_active = False
        if last_post:
            try:
                dt = datetime.fromisoformat(last_post.replace("Z", "+00:00"))
                days = (datetime.now(timezone.utc) - dt).days
                has_active = days <= 60
            except ValueError:
                pass

        custom = ch_snippet.get("customUrl") or channel_id
        url = (f"https://www.youtube.com/{custom}"
               if custom.startswith("@") else f"https://www.youtube.com/channel/{channel_id}")

        return {
            "platform_handle": custom,
            "platform_url": url,
            "follower_count": int(stats.get("subscriberCount", 0) or 0),
            "has_active_content": has_active,
            "last_post_at": last_post,
        }
