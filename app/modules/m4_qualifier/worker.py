"""
M4 Qualifier Worker.

Pulls accounts where M3 has already run (gap_analysis row exists)
but qualified_prospects doesn't yet have them.

For each: compute the score, insert into qualified_prospects.
"""
from __future__ import annotations
import asyncio
import json
from app.core.supabase_client import get_supabase
from app.core.logging import configure_logging, get_logger
from .scorer import qualify, QUALIFIED_THRESHOLD, HIGH_VALUE_THRESHOLD

configure_logging()
log = get_logger("m4.worker")

BATCH_SIZE = 10
IDLE_POLL_SECONDS = 60
BETWEEN_ACCOUNTS_SECONDS = 2


def pick_accounts_for_qualification(limit: int = BATCH_SIZE) -> list[dict]:
    """
    Accounts that have a gap_analysis row but NOT a qualified_prospects row.
    Oldest gap_analysis first.
    """
    sb = get_supabase()
    # Get ids already qualified (to exclude)
    qp_rows = (sb.table("qualified_prospects")
               .select("account_id")
               .execute()).data or []
    excluded = [r["account_id"] for r in qp_rows]

    # Get accounts that DO have gap_analysis
    analyzed = (sb.table("gap_analysis")
                .select("account_id")
                .execute()).data or []
    candidate_ids = [r["account_id"] for r in analyzed if r["account_id"] not in excluded]

    if not candidate_ids:
        return []

    accounts = (sb.table("accounts")
                .select("id,handle,full_name,bio,follower_count,external_url,"
                        "depth,discovered_via,discovered_from")
                .in_("id", candidate_ids[:limit])
                .execute()).data or []

    return accounts


def get_gap_for(account_id: str) -> dict | None:
    sb = get_supabase()
    r = (sb.table("gap_analysis")
         .select("*")
         .eq("account_id", account_id)
         .limit(1)
         .execute()).data
    return r[0] if r else None


def get_cross_platform_for(account_id: str) -> list[dict]:
    """Returns ALL platform rows (one per platform: tiktok, youtube, etc.)."""
    sb = get_supabase()
    r = (sb.table("cross_platform_profiles")
         .select("*")
         .eq("account_id", account_id)
         .execute()).data
    return r or []


def write_qualification(account: dict, gap: dict | None, result: dict) -> None:
    sb = get_supabase()

    # Determine status. is_qualified already accounts for celebrity DQ
    # (scorer.qualify() forces it to False when is_celebrity_disqualified),
    # so a single branch covers both "low score" and "celeb" dead paths.
    if not result["is_qualified"]:
        status = "dead"
    else:
        status = "pending_warmup"

    payload = {
        "account_id": account["id"],
        "handle": account["handle"],
        "pre_filter_score": result["pre_filter_score"],
        "link_crawl_score": result["link_crawl_score"],
        "cross_platform_score": result["cross_platform_score"],
        "total_score": result["total_score"],
        "link_in_bio": account.get("external_url"),
        "link_resolved_to": (gap or {}).get("fetched_url"),
        "is_qualified": result["is_qualified"],
        "is_high_value": result["is_high_value"],
        "high_value_reason": result["high_value_reason"],
        "is_celebrity_disqualified": result["is_celebrity_disqualified"],
        "celebrity_dq_reason": result["celebrity_dq_reason"],
        "status": status,
    }

    sb.table("qualified_prospects").insert(payload).execute()

    # Also stash primary_gaps + discovery_source onto gap_analysis if present
    if gap and result["primary_gaps"]:
        sb.table("gap_analysis").update({
            "primary_gap": result["primary_gaps"][0],
            "cross_platform_discovery_source": result["cross_platform_discovery_source"],
        }).eq("account_id", account["id"]).execute()


async def process_one(account: dict) -> None:
    gap = get_gap_for(account["id"])
    cp = get_cross_platform_for(account["id"])
    result = qualify(account, gap, cp)

    # Loud log when the celebrity disqualifier fires — separate from the
    # general qualify log so it's grep-able in worker_qual output.
    if result["is_celebrity_disqualified"]:
        log.info(
            "m4.celebrity_disqualified",
            handle=account["handle"],
            follower_count=account.get("follower_count"),
            total_would_be=result["total_score"],
            reason=result["celebrity_dq_reason"],
        )

    log.info(
        "m4.qualify",
        handle=account["handle"],
        total=result["total_score"],
        pre=result["pre_filter_score"],
        link=result["link_crawl_score"],
        cp=result["cross_platform_score"],
        qualified=result["is_qualified"],
        high_value=result["is_high_value"],
        celebrity_dq=result["is_celebrity_disqualified"],
        primary_gaps=result["primary_gaps"],
    )

    write_qualification(account, gap, result)


async def worker_loop():
    log.info("m4.worker.start",
             qualified_threshold=QUALIFIED_THRESHOLD,
             high_value_threshold=HIGH_VALUE_THRESHOLD)
    while True:
        accounts = pick_accounts_for_qualification()
        if not accounts:
            log.info("m4.worker.idle")
            await asyncio.sleep(IDLE_POLL_SECONDS)
            continue

        log.info("m4.worker.batch", size=len(accounts))
        for acct in accounts:
            try:
                await process_one(acct)
            except Exception as e:
                log.error("m4.worker.unhandled",
                          handle=acct.get("handle"), err=str(e), exc_info=True)
            await asyncio.sleep(BETWEEN_ACCOUNTS_SECONDS)


if __name__ == "__main__":
    asyncio.run(worker_loop())
