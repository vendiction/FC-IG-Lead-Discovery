"""Repository for the accounts table — upserts with dedup, edge tracking."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional
from app.core.supabase_client import get_supabase
from app.core.logging import get_logger

log = get_logger(__name__)


def upsert_account(profile: dict, discovered_via: str,
                   discovered_from: Optional[str] = None,
                   depth: int = 0) -> dict:
    """
    Insert or update an account record. Returns the stored row.

    `profile` is the dict from ig_api.normalize_profile().
    Strips the internal _pk before insert.
    """
    sb = get_supabase()

    pk = profile.pop("_pk", None)
    record = dict(profile)
    record["discovered_via"] = discovered_via
    record["discovered_from"] = discovered_from
    record["depth"] = depth
    record["last_refreshed_at"] = datetime.now(timezone.utc).isoformat()

    # Check if exists
    existing = (sb.table("accounts").select("id,depth,discovered_via")
                .eq("handle", record["handle"]).execute()).data

    if existing:
        # Update but preserve shortest depth + earliest discovery
        cur = existing[0]
        if depth < cur["depth"]:
            record["depth"] = depth
        else:
            record.pop("depth")
            record.pop("discovered_via", None)
            record.pop("discovered_from", None)
        result = (sb.table("accounts").update(record)
                  .eq("id", cur["id"]).execute()).data
        return result[0] if result else cur
    else:
        result = sb.table("accounts").insert(record).execute().data
        return result[0]


def insert_tag_edge(from_handle: str, to_handle: str,
                    via_post_url: Optional[str] = None) -> bool:
    """Insert a tag-discovery edge. Returns False if dupe."""
    sb = get_supabase()
    try:
        sb.table("tag_edges").insert({
            "from_handle": from_handle.lower(),
            "to_handle": to_handle.lower(),
            "via_post_url": via_post_url,
        }).execute()
        return True
    except Exception as e:
        if "duplicate key" in str(e).lower():
            return False
        log.warning("tag_edge.insert_failed", err=str(e))
        return False


def account_exists(handle: str) -> bool:
    sb = get_supabase()
    r = (sb.table("accounts").select("id", count="exact")
         .eq("handle", handle.lstrip("@").lower())
         .limit(1).execute())
    return (r.count or 0) > 0
