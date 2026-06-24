"""
M6 Opener Generator Worker.

Polls qualified_prospects where status='pending_warmup' and no opener exists yet.
For each: pick archetype, build prompt, dispatch LLM, validate, write opener.

In manual_paste mode this is INTERACTIVE — operator must paste the response.
Run from terminal foreground, NOT as a detached docker service.
"""
from __future__ import annotations
import asyncio
from typing import Optional
from app.core.supabase_client import get_supabase
from app.core.logging import configure_logging, get_logger
from .prompt_builder import (
    pick_archetype, get_templates, get_curiosity_phrases,
    get_anti_patterns, get_prior_openers, build_user_prompt, SYSTEM_PROMPT,
)
from .generator import generate, validate_opener

configure_logging()
log = get_logger("m6.worker")

MAX_RETRIES_PER_PROSPECT = 3
BATCH_SIZE = 5
IDLE_POLL_SECONDS = 60
BETWEEN_PROSPECTS_SECONDS = 2


# ────────────────────────────────────────────────────────────────────
# DB queries
# ────────────────────────────────────────────────────────────────────

def pick_prospects_needing_openers(limit: int = BATCH_SIZE) -> list[dict]:
    """
    Qualified prospects with status='pending_warmup' that don't yet have an opener.
    """
    sb = get_supabase()
    # Prospects with at least one opener already
    have_opener = (sb.table("openers").select("prospect_id").execute()).data or []
    excluded = list({r["prospect_id"] for r in have_opener if r.get("prospect_id")})

    q = (sb.table("qualified_prospects")
         .select("id,account_id,handle,total_score,is_high_value")
         .in_("status", ["pending_warmup", "warmed", "pending_opener"]))
    if excluded:
        q = q.not_.in_("id", excluded)
    prospects = (q.limit(limit).execute()).data or []
    return prospects


def hydrate(prospect: dict) -> dict:
    """Pull account + gap_analysis + cross_platform_profiles for a prospect."""
    sb = get_supabase()
    acct_rows = (sb.table("accounts")
                 .select("id,handle,full_name,bio,follower_count,external_url")
                 .eq("id", prospect["account_id"]).limit(1).execute()).data or []
    account = acct_rows[0] if acct_rows else {}

    gap_rows = (sb.table("gap_analysis")
                .select("primary_gap,cross_platform_discovery_source,gap_evidence,"
                        "gap_homepage_conversion,gap_lead_magnet_missing,"
                        "gap_email_revenue_underperform,gap_content_struggle,"
                        "gap_product_page_competitor,gap_local_seo,has_email_capture")
                .eq("account_id", prospect["account_id"]).limit(1).execute()).data or []
    gap = gap_rows[0] if gap_rows else {}

    return {"prospect": prospect, "account": account, "gap": gap}


def first_name_from(account: dict) -> Optional[str]:
    full = (account.get("full_name") or "").strip()
    if not full:
        return None
    return full.split()[0]


def gap_evidence_text(gap: dict) -> Optional[str]:
    """Turn the gap booleans into a short hook description for the LLM."""
    bits = []
    if not gap.get("has_email_capture"):
        bits.append("no email capture on landing page")
    if gap.get("gap_homepage_conversion"):
        bits.append("homepage isn't conversion-focused")
    if gap.get("gap_lead_magnet_missing"):
        bits.append("no lead magnet visible")
    if gap.get("gap_content_struggle"):
        bits.append("light content presence")
    if gap.get("gap_product_page_competitor"):
        bits.append("product page weaker than competitors")
    return "; ".join(bits) if bits else None


# ────────────────────────────────────────────────────────────────────
# Per-prospect process
# ────────────────────────────────────────────────────────────────────

async def process_one(prospect: dict) -> None:
    bundle = hydrate(prospect)
    account = bundle["account"]
    gap = bundle["gap"]

    cps = gap.get("cross_platform_discovery_source")
    primary_gap = gap.get("primary_gap")
    primary_gaps = [primary_gap] if primary_gap else []
    bio = account.get("bio")

    archetype = pick_archetype(cps, primary_gaps, bio)
    log.info("m6.archetype.picked",
             handle=prospect["handle"], archetype=archetype,
             primary_gap=primary_gap, cps=cps)

    templates = get_templates(archetype, limit=5)
    curiosity = get_curiosity_phrases(limit=5)
    anti = get_anti_patterns()
    prior = get_prior_openers(prospect["id"])

    user_prompt = build_user_prompt(
        archetype=archetype,
        prospect_handle=prospect["handle"],
        prospect_first_name=first_name_from(account),
        bio=bio,
        cross_platform_source=cps,
        primary_gap=primary_gap,
        gap_evidence=gap_evidence_text(gap),
        templates=templates,
        curiosity_phrases=curiosity,
        anti_patterns=anti,
        prior_openers=prior,
    )

    # Try up to MAX_RETRIES_PER_PROSPECT
    for attempt in range(1, MAX_RETRIES_PER_PROSPECT + 1):
        log.info("m6.generate.start",
                 handle=prospect["handle"], attempt=attempt, archetype=archetype)
        try:
            text, meta = generate(SYSTEM_PROMPT, user_prompt, prospect["handle"])
        except Exception as e:
            log.error("m6.generate.exception", handle=prospect["handle"], err=str(e))
            return

        result = validate_opener(text, prior_openers=prior)
        if result.valid:
            log.info("m6.generate.valid",
                     handle=prospect["handle"], attempt=attempt,
                     char_count=result.char_count,
                     uses_ellipsis=result.uses_ellipsis,
                     ends_with_question=result.ends_with_question)
            write_opener(
                prospect_id=prospect["id"],
                opener_text=text,
                archetype=archetype,
                primary_gap=primary_gap,
                validation=result,
                meta=meta,
            )
            return
        else:
            log.warning("m6.generate.invalid",
                        handle=prospect["handle"], attempt=attempt,
                        fails=result.fails, text_preview=text[:120])

    log.error("m6.generate.gave_up",
              handle=prospect["handle"], attempts=MAX_RETRIES_PER_PROSPECT)


# ────────────────────────────────────────────────────────────────────
# DB write
# ────────────────────────────────────────────────────────────────────

def write_opener(*, prospect_id: str, opener_text: str, archetype: str,
                 primary_gap: Optional[str], validation, meta: dict) -> None:
    sb = get_supabase()
    payload = {
        "prospect_id": prospect_id,
        "opener_text": opener_text,
        "archetype": archetype,
        "char_count": validation.char_count,
        "fits_lockscreen_preview": validation.char_count <= 130,
        "uses_ellipsis": validation.uses_ellipsis,
        "sipe_short": validation.char_count <= 160,
        "sipe_incomplete": validation.uses_ellipsis or validation.ends_with_question,
        "sipe_personal": True,   # by construction (prompt enforces it)
        "sipe_emotional": True,
        "hooked_on_gap": primary_gap,
        "claude_model": meta.get("raw", {}).get("model") if meta.get("mode") == "api" else meta.get("model"),
        "claude_raw_response": meta if meta.get("mode") == "api" else None,
        "approved_for_send": False,    # always human-approve in V1
    }
    sb.table("openers").insert(payload).execute()


# ────────────────────────────────────────────────────────────────────
# Main loop
# ────────────────────────────────────────────────────────────────────

async def worker_loop():
    log.info("m6.worker.start", max_retries=MAX_RETRIES_PER_PROSPECT)
    while True:
        prospects = pick_prospects_needing_openers()
        if not prospects:
            log.info("m6.worker.idle")
            await asyncio.sleep(IDLE_POLL_SECONDS)
            continue
        log.info("m6.worker.batch", size=len(prospects))
        for p in prospects:
            try:
                await process_one(p)
            except Exception as e:
                log.error("m6.worker.unhandled",
                          handle=p.get("handle"), err=str(e), exc_info=True)
            await asyncio.sleep(BETWEEN_PROSPECTS_SECONDS)


if __name__ == "__main__":
    asyncio.run(worker_loop())
