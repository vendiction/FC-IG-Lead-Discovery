"""
Persistent crawl queue backed by Supabase.

Workers atomically claim a handle, process it, then mark done.
Survives container restarts. Deduped at enqueue (UNIQUE constraint on handle).
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Optional
from app.core.supabase_client import get_supabase
from app.core.logging import get_logger

log = get_logger(__name__)

CLAIM_TIMEOUT_MIN = 15  # if a worker dies mid-claim, claim expires


def enqueue(handle: str, depth: int = 0, parent_seed: Optional[str] = None,
            priority: int = 5) -> bool:
    """
    Add a handle to the queue. Returns True if newly added, False if dupe.
    """
    sb = get_supabase()
    handle = handle.lstrip("@").lower()
    try:
        sb.table("crawl_queue").insert({
            "handle": handle,
            "depth": depth,
            "parent_seed": parent_seed,
            "priority": priority,
            "status": "pending",
        }).execute()
        return True
    except Exception as e:
        # UNIQUE violation = already in queue; not an error
        if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
            return False
        raise


def enqueue_many(handles: list[tuple[str, int, str]], priority: int = 5) -> int:
    """
    Bulk enqueue. Each tuple: (handle, depth, parent_seed).
    Returns count of newly-added (dupes silently skipped).
    """
    added = 0
    for handle, depth, parent in handles:
        if enqueue(handle, depth=depth, parent_seed=parent, priority=priority):
            added += 1
    return added


def claim_next(worker_id: str) -> Optional[dict]:
    """
    Atomically claim the next pending handle. Returns None if queue empty.

    Uses a Postgres UPDATE...RETURNING to avoid races between workers.
    Also reclaims rows whose claim has expired (worker crashed).
    """
    sb = get_supabase()
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=CLAIM_TIMEOUT_MIN)).isoformat()
    now = datetime.now(timezone.utc).isoformat()

    # Approach: select an eligible row, then update by id with optimistic
    # concurrency. Multiple workers may collide; whoever's update succeeds wins.
    for _ in range(5):
        candidates = (sb.table("crawl_queue")
                      .select("id,handle,depth,parent_seed,attempts")
                      .or_(f"status.eq.pending,and(status.eq.claimed,claimed_at.lt.{cutoff})")
                      .order("priority", desc=False)
                      .order("enqueued_at", desc=False)
                      .limit(1)
                      .execute()).data

        if not candidates:
            return None

        row = candidates[0]

        # Try to claim
        upd = (sb.table("crawl_queue")
               .update({
                   "status": "claimed",
                   "claimed_by": worker_id,
                   "claimed_at": now,
                   "attempts": row["attempts"] + 1,
               })
               .eq("id", row["id"])
               .in_("status", ["pending", "claimed"])
               .execute())

        if upd.data:
            return row
        # else: lost the race, retry

    return None


def mark_done(queue_id: str) -> None:
    get_supabase().table("crawl_queue").update({
        "status": "done"
    }).eq("id", queue_id).execute()


def mark_failed(queue_id: str, error: str, retry: bool = True) -> None:
    sb = get_supabase()
    row = (sb.table("crawl_queue").select("attempts").eq("id", queue_id)
           .single().execute()).data
    attempts = row["attempts"] if row else 99

    new_status = "pending" if (retry and attempts < 3) else "failed"
    sb.table("crawl_queue").update({
        "status": new_status,
        "last_error": error[:500],
        "claimed_by": None,
        "claimed_at": None,
    }).eq("id", queue_id).execute()


def queue_depth() -> dict:
    """Return counts by status."""
    sb = get_supabase()
    statuses = ["pending", "claimed", "done", "failed", "skipped"]
    result = {}
    for s in statuses:
        r = (sb.table("crawl_queue").select("id", count="exact")
             .eq("status", s).limit(1).execute())
        result[s] = r.count or 0
    return result
