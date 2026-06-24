"""
M3 worker. Continuously pulls accounts that haven't been gap-analyzed
and runs research on them.

Decoupled from M1/M2: runs at its own pace, independent rate budget
(no IG operator accounts needed — only hits TikTok, YouTube API, websites).
"""
from __future__ import annotations
import asyncio
from app.core.supabase_client import get_supabase
from app.core.logging import configure_logging, get_logger
from .research import research_account

configure_logging()
log = get_logger("m3.worker")

BATCH_SIZE = 10
IDLE_POLL_SECONDS = 60
BETWEEN_ACCOUNTS_SECONDS = 5


def pick_accounts_for_research(limit: int = BATCH_SIZE) -> list[dict]:
    """Accounts without a gap_analysis row, oldest first."""
    sb = get_supabase()
    # NOT EXISTS via two-step query
    analyzed_ids = (sb.table("gap_analysis").select("account_id").execute()).data
    excluded = [r["account_id"] for r in analyzed_ids]

    q = (sb.table("accounts")
         .select("id,handle,full_name,bio,follower_count,external_url")
         .order("first_seen_at", desc=False)
         .limit(limit * 3))  # over-fetch since we filter client-side

    if excluded:
        # Supabase Python client supports .not_.in_()
        q = q.not_.in_("id", excluded[:1000])  # cap excluded list

    r = q.execute()
    return r.data[:limit] if r.data else []


async def worker_loop():
    log.info("m3.worker.start")
    while True:
        accounts = pick_accounts_for_research()
        if not accounts:
            log.info("m3.worker.idle")
            await asyncio.sleep(IDLE_POLL_SECONDS)
            continue

        log.info("m3.worker.batch", size=len(accounts))
        for acct in accounts:
            try:
                await research_account(acct)
            except Exception as e:
                log.error("m3.worker.unhandled",
                          handle=acct.get("handle"), err=str(e), exc_info=True)
            await asyncio.sleep(BETWEEN_ACCOUNTS_SECONDS)


if __name__ == "__main__":
    asyncio.run(worker_loop())
