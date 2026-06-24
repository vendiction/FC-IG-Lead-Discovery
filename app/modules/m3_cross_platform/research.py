"""
M3 Research orchestrator.

For each account that hasn't been researched yet:
1. Lookup TikTok presence
2. Lookup YouTube presence
3. If external_url exists, analyze website for gaps
4. Synthesize into a gap_analysis row + cross_platform_profiles rows

Picks primary_gap (the most exploitable one for M6's opener generator):
priority order:
  - email_revenue_underperform (e-com w/ no email capture)
  - lead_magnet_missing (coach/info w/ no lead magnet)
  - homepage_conversion (any w/ weak homepage)
  - product_page_competitor (e-com)
  - local_seo
  - content_struggle
  - cross_platform_mismatch (TikTok or YouTube exists but IG missing it)

Also flags cross_platform_discovery_source if a non-IG platform has more
followers than IG — that signals "they're on TikTok, hitting them on IG"
is the right opener angle.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional
from app.core.supabase_client import get_supabase
from app.core.logging import get_logger
from .tiktok import find_tiktok
from .youtube import find_youtube
from .website import analyze_website

log = get_logger(__name__)


def _select_primary_gap(g: dict, account: dict) -> tuple[Optional[str], Optional[str]]:
    """
    Decide which gap is the strongest hook for the opener.
    Returns (gap_name, evidence_string).
    """
    ev = g.get("evidence", {})

    # E-com + no email capture = Mason's #1 gap ("clueless about email")
    if g.get("is_ecom") and not g.get("has_email_capture"):
        return ("email_revenue_underperform",
                "e-com site with no detectable email capture — likely under-monetizing list")

    # Lead magnet missing on a coaching/info profile
    bio = (account.get("bio") or "").lower()
    is_coach_info = any(kw in bio for kw in [
        "coach", "consultant", "expert", "course", "mentor", "advisor", "trainer",
    ])
    if is_coach_info and g.get("gap_lead_magnet_missing"):
        return ("lead_magnet_missing",
                "coaching/info profile with no lead magnet detected on landing page")

    if g.get("gap_homepage_conversion"):
        return ("homepage_conversion", ev.get("homepage", "weak homepage hero"))

    if g.get("gap_product_page_competitor"):
        return ("product_page_competitor", ev.get("product_page", "product page gap"))

    if g.get("gap_local_seo"):
        return ("local_seo", ev.get("local_seo", "local SEO gap"))

    if g.get("gap_content_struggle"):
        return ("content_struggle", "no blog or newsletter detected — content gap")

    return (None, None)


async def research_account(account: dict) -> Optional[dict]:
    """
    Run the full cross-platform research pass for one account.

    Returns the gap_analysis row, or None if account had no external_url AND
    no cross-platform presence (truly thin — let M4 disqualify them).
    """
    sb = get_supabase()
    handle = account["handle"]
    full_name = account.get("full_name")
    bound_log = log.bind(handle=handle)
    bound_log.info("m3.research.start")

    # ----- Cross-platform lookups -----
    tt_info = None
    yt_info = None
    try:
        tt_info = await find_tiktok(handle, full_name)
    except Exception as e:
        bound_log.warning("m3.tiktok.failed", err=str(e))

    try:
        yt_info = await find_youtube(handle, full_name)
    except Exception as e:
        bound_log.warning("m3.youtube.failed", err=str(e))

    # ----- Persist cross_platform_profiles rows -----
    if tt_info:
        sb.table("cross_platform_profiles").insert({
            "account_id": account["id"],
            "handle": handle,
            "platform": "tiktok",
            "platform_handle": tt_info["platform_handle"],
            "platform_url": tt_info["platform_url"],
            "follower_count": tt_info.get("follower_count"),
            "has_active_content": tt_info.get("has_active_content"),
            "raw_data": tt_info,
        }).execute()

    if yt_info:
        sb.table("cross_platform_profiles").insert({
            "account_id": account["id"],
            "handle": handle,
            "platform": "youtube",
            "platform_handle": yt_info["platform_handle"],
            "platform_url": yt_info["platform_url"],
            "follower_count": yt_info.get("follower_count"),
            "has_active_content": yt_info.get("has_active_content"),
            "last_post_at": yt_info.get("last_post_at"),
            "raw_data": yt_info,
        }).execute()

    # ----- Website analysis -----
    web = await analyze_website(account.get("external_url"))

    # ----- Cross-platform discovery source -----
    # If a non-IG platform has > IG followers, that's the Mason opener angle
    ig_followers = account.get("follower_count") or 0
    cps = None
    if tt_info and tt_info.get("follower_count", 0) > ig_followers and tt_info.get("has_active_content"):
        cps = "tiktok"
    elif yt_info and yt_info.get("follower_count", 0) > ig_followers and yt_info.get("has_active_content"):
        cps = "youtube"

    # ----- Pick primary gap -----
    primary_gap, evidence = _select_primary_gap(web, account)
    if not primary_gap and cps:
        primary_gap = "cross_platform_mismatch"
        evidence = f"strong on {cps}, weaker on IG — opener angle"

    # ----- Upsert gap_analysis -----
    existing = (sb.table("gap_analysis").select("id")
                .eq("account_id", account["id"]).execute()).data

    record = {
        "account_id": account["id"],
        "has_website": web["has_website"],
        "has_email_capture": web["has_email_capture"],
        "has_lead_magnet": web["has_lead_magnet"],
        "has_paid_offer": web["has_paid_offer"],
        "has_youtube": bool(yt_info),
        "has_tiktok": bool(tt_info),
        "primary_gap": primary_gap,
        "gap_evidence": evidence,
        "gap_local_seo": web["gap_local_seo"],
        "gap_homepage_conversion": web["gap_homepage_conversion"],
        "gap_product_page_competitor": web["gap_product_page_competitor"],
        "gap_email_revenue_underperform":
            web["is_ecom"] and not web["has_email_capture"],
        "gap_lead_magnet_missing": web["gap_lead_magnet_missing"],
        "gap_content_struggle": web["gap_content_struggle"],
        "cross_platform_discovery_source": cps,
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
    }

    if existing:
        result = (sb.table("gap_analysis").update(record)
                  .eq("id", existing[0]["id"]).execute()).data
    else:
        result = sb.table("gap_analysis").insert(record).execute().data

    bound_log.info("m3.research.done",
                   has_tiktok=bool(tt_info),
                   has_youtube=bool(yt_info),
                   has_website=web["has_website"],
                   primary_gap=primary_gap,
                   cps=cps)
    return result[0] if result else None
