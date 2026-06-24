"""
M4 Qualifier — Scoring logic.

Takes an account + its gap_analysis + cross_platform_profile rows
and returns a 0-100 total score broken into three components.

Mason's emphasis is on:
  - Real business owners (not influencers/celebrities)
  - With a website
  - Where we can find a SPECIFIC named gap (not generic "you could be better")
  - Cross-platform presence (TikTok + IG + YouTube triangle)

So scoring weights heavily toward:
  - Business signals in bio
  - At least one Mason-named gap in their website
  - Cross-platform discoverability (their content is findable on other platforms)
"""
from __future__ import annotations
from typing import Optional


# ────────────────────────────────────────────────────────────────────
# Pre-filter score (0–30) — from the IG profile alone
# ────────────────────────────────────────────────────────────────────

# Hard disqualifier — anyone above this is forced is_qualified=False regardless of
# total_score. Mirrors M6 sender's OPENER_SEND_MAX_FOLLOWER_COUNT so the scorer
# and the sender agree on what's a celebrity. Tune both together.
FOLLOWER_HARD_DQ_CAP = 500_000


BUSINESS_BIO_KEYWORDS = [
    # Business operator signals
    "founder", "ceo", "owner", "entrepreneur", "co-founder", "cofounder",
    "creator of", "built", "building", "launching", "growing",
    # Service/coach signals (the bulk of Mason's targets)
    "coach", "consultant", "mentor", "strategist", "expert",
    "help", "helping", "i help",
    # Concrete offer signals
    "course", "program", "agency", "studio", "podcast", "newsletter",
    "book", "ebook", "free guide", "dm me",
]

NEGATIVE_BIO_SIGNALS = [
    # Pure influencer / celebrity / fan / brand patterns we want to deprioritize
    "official", "verified account", "for inquiries email",
    "represented by", "managed by", "official partner",
    "manager:", "booking:", "press:",
    # Personal/lifestyle accounts
    "mom of", "dad of", "wife", "husband", "boyfriend", "girlfriend",
    "just here for",
]


def score_pre_filter(account: dict) -> tuple[int, list[str]]:
    """
    Score the IG account itself, before we even visit their website.

    Returns (score 0-30, list of reasons for scoring).
    """
    reasons: list[str] = []
    score = 0
    bio = (account.get("bio") or "").lower()
    handle = (account.get("handle") or "").lower()
    full_name = (account.get("full_name") or "").lower()
    follower_count = account.get("follower_count") or 0
    external_url = account.get("external_url")

    # ── Bio business signals ──
    biz_hits = [k for k in BUSINESS_BIO_KEYWORDS if k in bio]
    if biz_hits:
        score += min(10, len(biz_hits) * 3)
        reasons.append(f"bio business keywords: {biz_hits[:4]}")

    # ── Bio negative signals (subtract) ──
    neg_hits = [k for k in NEGATIVE_BIO_SIGNALS if k in bio]
    if neg_hits:
        score -= min(8, len(neg_hits) * 4)
        reasons.append(f"bio negative signals: {neg_hits[:3]}")

    # ── Has link in bio ──
    if external_url:
        score += 6
        reasons.append("has link in bio")

    # ── Follower count sweet spot ──
    # Mason's targets are small-to-medium operators, NOT celebrities.
    # 1k-200k          = sweet spot,             +6
    # 200k-500k        = above sweet but OK,     +1   (M6 will still send)
    # 500k-1M          = M6 will REFUSE,        -15   (no point qualifying)
    # >1M              = celebrity territory,   -25   (Mason explicit DQ)
    # <500             = bot/dead,               -3
    #
    # 500K is the M6 sender's preflight cap (FOLLOWER_HARD_DQ_CAP). Anyone
    # past that is also hit by the hard disqualifier in qualify() — the
    # negative score here is belt-and-suspenders so total_score also reflects
    # priority for any downstream consumer that ignores is_qualified.
    if 1_000 <= follower_count <= 200_000:
        score += 6
        reasons.append(f"sweet-spot followers ({follower_count})")
    elif 200_000 < follower_count <= FOLLOWER_HARD_DQ_CAP:
        score += 1
        reasons.append(f"above sweet spot but workable ({follower_count})")
    elif FOLLOWER_HARD_DQ_CAP < follower_count <= 1_000_000:
        score -= 15
        reasons.append(f"will be M6-refused ({follower_count})")
    elif follower_count > 1_000_000:
        score -= 25
        reasons.append(f"celebrity-tier — Mason DQ ({follower_count})")
    elif follower_count < 500:
        score -= 3
        reasons.append(f"tiny follower count ({follower_count})")

    # ── Discovered via tagged (not seed) — slight bonus, depth-1+ ──
    if account.get("depth", 0) >= 1 and account.get("discovered_via") == "tagged":
        score += 4
        reasons.append("rabbit-hole discovered, not seed")

    # Clamp to 0..30
    score = max(0, min(30, score))
    return score, reasons


# ────────────────────────────────────────────────────────────────────
# Link-crawl score (0–40) — from gap_analysis row
# ────────────────────────────────────────────────────────────────────

# Mason's named gaps, in priority order (his actual emphasis).
# Each gap that EXISTS in the prospect's site = points for us.
GAP_WEIGHTS = {
    "gap_homepage_conversion": 10,         # "one line on the homepage killing you"
    "gap_email_revenue_underperform": 10,  # "20-30% of revenue should come from email"
    "gap_lead_magnet_missing": 7,          # "no opt-in, no list"
    "gap_product_page_competitor": 8,      # e-com only — "competitors have this you don't"
    "gap_local_seo": 5,                    # local biz specific
    "gap_content_struggle": 3,             # blog/newsletter missing
}


def score_link_crawl(gap: Optional[dict]) -> tuple[int, list[str], list[str]]:
    """
    Score the prospect's landing page.

    Returns (score 0-40, list of reasons, list of named primary gaps).
    """
    reasons: list[str] = []
    primary_gaps: list[str] = []
    score = 0

    if not gap:
        return 0, ["no gap_analysis row yet"], []

    if not gap.get("has_website"):
        # Mason: no website = nothing to hook into → strong disqualifier
        return 0, ["no website at all (Mason DQ)"], []

    # Baseline for having a fetched site
    score += 5
    reasons.append("has website (fetched ok)")

    # Has paid offer is a positive signal — they have something to SELL.
    # No paid offer doesn't disqualify (they may sell via DM), but mark.
    if gap.get("has_paid_offer"):
        score += 5
        reasons.append("has visible paid offer")
    else:
        reasons.append("no visible paid offer")

    # Email capture present = signals they understand list-building.
    # Counter-intuitive but: if they HAVE email capture, the email_revenue_underperform
    # gap is less hookable. We score gap_lead_magnet_missing only when missing.
    if not gap.get("has_email_capture"):
        # This is the "you don't have email capture" hook
        primary_gaps.append("email_revenue_underperform")
        reasons.append("NO email capture (Mason gap: email revenue)")

    # Walk named gaps
    for gap_key, weight in GAP_WEIGHTS.items():
        # Skip the email gap above — we score it differently
        if gap_key == "gap_email_revenue_underperform":
            if not gap.get("has_email_capture"):
                score += weight
            continue
        # Skip ecom-only gap if not ecom
        if gap_key == "gap_product_page_competitor" and not gap.get("is_ecom"):
            continue
        # Skip local SEO if no local signals
        if gap_key == "gap_local_seo" and not gap.get("gap_local_seo"):
            continue
        if gap.get(gap_key):
            score += weight
            reasons.append(f"hit {gap_key} (+{weight})")
            primary_gaps.append(gap_key.replace("gap_", ""))

    # Clamp 0..40
    score = max(0, min(40, score))
    return score, reasons, primary_gaps


# ────────────────────────────────────────────────────────────────────
# Cross-platform score (0–30) — from cross_platform_profiles row
# ────────────────────────────────────────────────────────────────────


def score_cross_platform(cp_rows: Optional[list]) -> tuple[int, list[str], Optional[str]]:
    """
    Score the cross-platform presence.

    cp_rows is a LIST of rows from cross_platform_profiles, one per platform.
    Real schema: platform ('tiktok'|'youtube'|...), platform_handle, platform_url,
                 follower_count, has_active_content, last_post_at.

    Returns (score 0-30, reasons, suggested cross_platform_discovery_source).
    The discovery source is the OTHER platform we can mention in the opener
    ("Saw you on TikTok, hitting you on IG").
    """
    reasons: list[str] = []
    score = 0
    suggested_source: Optional[str] = None

    if not cp_rows:
        return 0, ["no cross_platform_profiles rows yet"], None

    has_tiktok = False
    has_youtube = False
    tiktok_active = False
    youtube_active = False

    for row in cp_rows:
        platform = (row.get("platform") or "").lower()
        if platform == "tiktok" and row.get("platform_handle"):
            has_tiktok = True
            if row.get("has_active_content"):
                tiktok_active = True
        elif platform == "youtube" and row.get("platform_handle"):
            has_youtube = True
            if row.get("has_active_content"):
                youtube_active = True

    # Each platform we found them on = points
    if has_tiktok:
        score += 12
        reasons.append("found on TikTok")
        suggested_source = "tiktok"
    if has_youtube:
        score += 10
        reasons.append("found on YouTube")
        if not suggested_source:
            suggested_source = "youtube"

    # Active recent content (M3 sets has_active_content)
    if tiktok_active:
        score += 4
        reasons.append("TikTok actively posting")
    if youtube_active:
        score += 4
        reasons.append("YouTube actively posting")

    score = max(0, min(30, score))
    return score, reasons, suggested_source


# ────────────────────────────────────────────────────────────────────
# Top-level: full qualification
# ────────────────────────────────────────────────────────────────────

QUALIFIED_THRESHOLD = 40    # below this = don't bother
HIGH_VALUE_THRESHOLD = 70   # above this = M8 should consider human handoff fast


def qualify(
    account: dict,
    gap: Optional[dict],
    cross_platform: Optional[dict],
) -> dict:
    """
    Run the full qualification and return a dict ready to insert into
    qualified_prospects.

    Args:
        account: row from accounts table
        gap: row from gap_analysis (or None)
        cross_platform: row from cross_platform_profiles (or None)

    Returns:
        dict with: pre_filter_score, link_crawl_score, cross_platform_score,
        total_score, is_qualified, is_high_value, primary_gaps, reasons,
        cross_platform_discovery_source.
    """
    pre_score, pre_reasons = score_pre_filter(account)
    link_score, link_reasons, primary_gaps = score_link_crawl(gap)
    cp_score, cp_reasons, cp_source = score_cross_platform(cross_platform)

    total = pre_score + link_score + cp_score

    # Hard celebrity disqualifier — independent of total_score. Matches M6
    # sender's preflight cap, so we don't waste M6 generation budget on
    # accounts the sender will refuse anyway.
    follower_count = account.get("follower_count") or 0
    is_celebrity_disqualified = follower_count > FOLLOWER_HARD_DQ_CAP
    celebrity_dq_reason: Optional[str] = None
    if is_celebrity_disqualified:
        celebrity_dq_reason = (
            f"follower_count {follower_count} exceeds FOLLOWER_HARD_DQ_CAP "
            f"({FOLLOWER_HARD_DQ_CAP})"
        )

    is_qualified = (total >= QUALIFIED_THRESHOLD) and not is_celebrity_disqualified
    is_high_value = (total >= HIGH_VALUE_THRESHOLD) and not is_celebrity_disqualified

    high_value_reason = None
    if is_high_value:
        # Pick the strongest single signal as the reason
        if primary_gaps:
            high_value_reason = f"high score ({total}) + named gap: {primary_gaps[0]}"
        elif cp_source:
            high_value_reason = f"high score ({total}) + cross-platform on {cp_source}"
        else:
            high_value_reason = f"high score ({total})"

    return {
        "pre_filter_score": pre_score,
        "link_crawl_score": link_score,
        "cross_platform_score": cp_score,
        "total_score": total,
        "is_qualified": is_qualified,
        "is_high_value": is_high_value,
        "high_value_reason": high_value_reason,
        "is_celebrity_disqualified": is_celebrity_disqualified,
        "celebrity_dq_reason": celebrity_dq_reason,
        "primary_gaps": primary_gaps,
        "cross_platform_discovery_source": cp_source,
        "reasons": {
            "pre_filter": pre_reasons,
            "link_crawl": link_reasons,
            "cross_platform": cp_reasons,
        },
    }
