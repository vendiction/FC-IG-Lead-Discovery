"""
M6 Opener Generator — Prompt Builder.

Pulls Mason's verbatim templates from mason_corpus, picks the right archetype
for this prospect, and builds the full LLM prompt (system + user).

The prompt is designed to make Claude produce an opener that passes Mason's
hard rules: ≤160 chars, ends with ellipsis or question, no anti-patterns,
no "I/me" overuse, unique per prospect.

Bug fixes (2026-06-23):
  1. The "for t in templates:" loop had no body — Mason's verbatim templates
     were fetched from the DB and silently discarded. Now properly rendered.
  2. SYSTEM_PROMPT now explicitly forbids raw gap-label strings in output
     (the "homepage_conversion" bug seen in the mrbeast opener).
"""
from __future__ import annotations
from typing import Literal, Optional
from app.core.supabase_client import get_supabase


Archetype = Literal["personal_hook", "gap_hook", "cross_platform_mismatch"]


# ────────────────────────────────────────────────────────────────────
# Archetype picker
# ────────────────────────────────────────────────────────────────────

def pick_archetype(
    cross_platform_source: Optional[str],
    primary_gaps: Optional[list[str]],
    bio: Optional[str],
) -> Archetype:
    """
    Mason's priority order:
    1. Cross-platform mismatch — strongest if we have it
    2. Gap hook — next best if we found a named gap
    3. Personal hook — fallback that uses something from their bio/posts
    """
    if cross_platform_source:
        return "cross_platform_mismatch"
    if primary_gaps:
        return "gap_hook"
    return "personal_hook"


# ────────────────────────────────────────────────────────────────────
# Corpus retrieval
# ────────────────────────────────────────────────────────────────────

def get_templates(archetype: Archetype, limit: int = 5) -> list[dict]:
    """Pull verbatim opener templates for this archetype from mason_corpus."""
    category = (
        "opener_cross_platform"
        if archetype == "cross_platform_mismatch"
        else f"opener_{archetype}"
    )
    sb = get_supabase()
    rows = (sb.table("mason_corpus")
            .select("id,text,notes")
            .eq("category", category)
            .eq("active", True)
            .limit(limit)
            .execute()).data or []
    return rows


def get_curiosity_phrases(limit: int = 5) -> list[dict]:
    """Pull Mason's curiosity-phrase library (used in all archetypes)."""
    sb = get_supabase()
    rows = (sb.table("mason_corpus")
            .select("id,text")
            .eq("category", "opener_curiosity_phrase")
            .eq("active", True)
            .limit(limit)
            .execute()).data or []
    return rows


def get_anti_patterns() -> list[str]:
    """Pull the explicit anti-pattern list for the LLM to avoid."""
    sb = get_supabase()
    rows = (sb.table("mason_corpus")
            .select("text")
            .eq("category", "anti_pattern")
            .eq("active", True)
            .execute()).data or []
    return [r["text"] for r in rows]


def get_prior_openers(prospect_id: str) -> list[str]:
    """Past openers sent to this prospect — must NOT generate something similar."""
    sb = get_supabase()
    rows = (sb.table("openers")
            .select("opener_text")
            .eq("prospect_id", prospect_id)
            .execute()).data or []
    return [r["opener_text"] for r in rows]


# ────────────────────────────────────────────────────────────────────
# Prompt assembly
# ────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You write Instagram DM openers in the voice of Mason — a copywriter who teaches "dangerously persuasive openers in 160 characters or less." Your job is to write ONE opener that gets a reply.

Mason's hard rules (NEVER break):
- 160 characters MAX, hard limit, count them
- Ends with an ellipsis "..." OR a question mark "?"
- NEVER starts with "Hey {first_name}, hope you're doing well" or any generic greeting that wastes the first 4 seconds
- NEVER more than 2 first-person pronouns (I, me, my, we, our) — this opener is about THEM
- NEVER promises results, NEVER pitches a service, NEVER mentions price, NEVER says "I help X do Y"
- NEVER use raw technical labels in the output (e.g. "homepage_conversion", "lead_magnet_missing", "email_revenue_underperform"). When a gap is referenced, translate it into natural English. E.g. "homepage_conversion" becomes "one line above the fold that might be costing you" or "something on your homepage". "lead_magnet_missing" becomes "no obvious place to grab a free thing". A raw label in the output is an automatic fail.
- Tone = "smart curious friend who noticed something" — NOT salesperson

What makes an opener work (S.I.P.E.):
- Short: under 160 chars
- Incomplete: leaves something unsaid that the reader has to ask about
- Personal: references something specific to this prospect — a post, a platform, a piece of their work, a gap on their site, a city, their offer
- Emotional: triggers curiosity, mild concern, or "wait, what?" — not validation or compliment

Output: just the opener text. No quotes, no explanation, no "Here's the opener:" preamble. Just the opener."""


def build_user_prompt(
    *,
    archetype: Archetype,
    prospect_handle: str,
    prospect_first_name: Optional[str],
    bio: Optional[str],
    cross_platform_source: Optional[str],
    primary_gap: Optional[str],
    gap_evidence: Optional[str],
    templates: list[dict],
    curiosity_phrases: list[dict],
    anti_patterns: list[str],
    prior_openers: list[str],
) -> str:
    """Build the per-prospect user message."""
    lines: list[str] = []
    lines.append(f"Write ONE opener (archetype: {archetype}) for this prospect.\n")

    # Prospect context
    lines.append("=== PROSPECT ===")
    lines.append(f"IG handle: @{prospect_handle}")
    if prospect_first_name:
        lines.append(f"First name: {prospect_first_name}")
    if bio:
        lines.append(f"Bio: {bio[:300]}")
    if cross_platform_source:
        lines.append(f"Also publishes on: {cross_platform_source}")
    if primary_gap:
        lines.append(f"Detected gap (technical label — DO NOT echo verbatim, translate to natural English): {primary_gap}")
    if gap_evidence:
        lines.append(f"Gap evidence: {gap_evidence[:200]}")
    lines.append("")

    # Mason's verbatim templates for THIS archetype
    if templates:
        lines.append(f"=== MASON'S VERBATIM {archetype.upper()} TEMPLATES ===")
        lines.append("(Use these as voice/structure reference. Do NOT copy verbatim. Mirror the rhythm.)")
        for t in templates:
            lines.append(f'  • "{t["text"]}"')
        lines.append("")

    # Curiosity-phrase library (cross-archetype)
    if curiosity_phrases:
        lines.append("=== CURIOSITY-PHRASE LIBRARY (use one of these or invent in this style) ===")
        for p in curiosity_phrases:
            lines.append(f'  • "{p["text"]}"')
        lines.append("")

    # Anti-patterns
    if anti_patterns:
        lines.append("=== ANTI-PATTERNS — DO NOT USE THESE ===")
        for ap in anti_patterns:
            lines.append(f"  ✗ {ap[:140]}")
        lines.append("")

    # Past openers to this prospect (avoid repeating)
    if prior_openers:
        lines.append("=== ALREADY SENT TO THIS PROSPECT — DO NOT REPEAT OR REPHRASE ===")
        for po in prior_openers:
            lines.append(f"  ✗ {po}")
        lines.append("")

    # Final instruction
    lines.append("=== TASK ===")
    if archetype == "cross_platform_mismatch":
        lines.append(
            f"Write a {archetype} opener for @{prospect_handle}. "
            f"Reference that you noticed them on {cross_platform_source}. "
            "Mason's reference example: 'Saw you on TikTok, but hitting you on IG…'. "
            "Mirror that energy — short, slightly mysterious, leaves them wondering why you switched platforms."
        )
    elif archetype == "gap_hook":
        lines.append(
            f"Write a {archetype} opener for @{prospect_handle}. "
            f"Reference the gap as a CURIOSITY ('noticed something on your site... worth asking about?'). "
            "Frame as a question, NEVER an accusation. Never tell them they're wrong or losing money — let them ask. "
            "Translate any technical gap label into plain English — never echo it verbatim in the output."
        )
    else:  # personal_hook
        lines.append(
            f"Write a {archetype} opener for @{prospect_handle}. "
            f"Reference something specific from their bio or content above. "
            "Get personal. One detail. Then drop a curiosity hook."
        )

    lines.append("")
    lines.append("Now write the opener. Output ONLY the opener text. No quotes, no preamble.")
    return "\n".join(lines)
