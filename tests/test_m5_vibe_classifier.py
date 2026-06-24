"""
Tests for M5 vibe classifier (heuristic phase only — no Anthropic calls).

Realistic sample profiles drawn from common IG patterns Mason's system would
encounter: fitness coaches, ecom founders, agency owners, lifestyle creators,
podcast hosts, etc.
"""
from __future__ import annotations
import os
import pytest

# Ensure LLM fallback is off for these tests
os.environ["M5_VIBE_USE_LLM"] = "false"

from app.modules.m5_warmup.vibe_classifier import classify_vibe
from app.modules.m5_warmup.vibe import VibeProfile
from app.modules.m5_warmup.comment_suggestions import template_for, COMMENT_TEMPLATES


# ────────────────────────────────────────────────────────────────────
# Casual samples
# ────────────────────────────────────────────────────────────────────

def test_emoji_heavy_lifestyle_creator_is_casual():
    p = classify_vibe(
        bio="✨ vibes only ✨ chasing the sun 🌊 mama of 2 👶👶 obsessed w coffee ☕",
        recent_captions=[
            "literally crying 😭😭😭 this view 🌅🔥",
            "BABY GIRL IS 6 MONTHS 🥹💕✨",
            "self care sunday vibes 🛁💆‍♀️ lets gooo",
            "🔥🔥🔥",
            "iykyk 💯",
        ],
    )
    assert p.vibe == "casual", f"expected casual, got {p.vibe} ({p.reasoning})"
    assert p.confidence > 0.6


def test_fitness_coach_short_hype_captions_is_casual():
    p = classify_vibe(
        bio="🏋️ online coach | helping ladies lose 20lbs 💪 free guide ⬇️",
        recent_captions=[
            "LFG 💪💪💪",
            "ladies you ain't ready for this one 🔥",
            "deadass the best leg day routine i've done",
            "MONDAY MOTIVATION 🚀",
        ],
    )
    assert p.vibe == "casual"
    assert p.confidence > 0.6


# ────────────────────────────────────────────────────────────────────
# Professional samples
# ────────────────────────────────────────────────────────────────────

def test_consultant_long_prose_no_emoji_is_professional():
    p = classify_vibe(
        bio=(
            "Founder & CEO at Sterling Strategy Group. "
            "Helping B2B SaaS companies scale from $1M to $10M ARR. "
            "Author of 'The Compound Customer'."
        ),
        recent_captions=[
            (
                "Most founders confuse positioning with messaging. Positioning is the "
                "structural choice about where you fit in the market. Messaging is how "
                "you communicate that choice. You cannot fix bad positioning with better "
                "copy. After working with 47 SaaS founders this year, I have seen this "
                "pattern repeat across every category we touched."
            ),
            (
                "Three lessons from running 12 customer interviews last week. First, "
                "what people say they want and what they actually pay for are rarely "
                "the same thing. Second, your churn signal lives in onboarding, not "
                "month six. Third, the highest-ARPU customers always come from referrals."
            ),
            (
                "The single biggest mistake I see in founder pitch decks is leading "
                "with the solution before establishing the problem context. Investors "
                "cannot evaluate the solution if they have not been convinced the "
                "problem is worth solving at scale."
            ),
        ],
    )
    assert p.vibe == "professional", f"expected professional, got {p.vibe} ({p.reasoning})"
    assert p.confidence > 0.65


def test_agency_owner_structured_minimal_emoji_is_professional():
    p = classify_vibe(
        bio="Owner — Henson Creative. We build brands for venture-backed startups.",
        recent_captions=[
            (
                "Brand systems fail when they are designed for the brand team instead "
                "of the operators who will use them daily. We rebuilt our token "
                "documentation three times before realizing the audience was wrong."
            ),
            (
                "Six common patterns in early-stage brand work that compound into "
                "rework later: inconsistent type scale, unbounded color systems, "
                "missing motion guidelines, no voice principles, undefined icon "
                "approach, and skipped accessibility audits."
            ),
        ],
    )
    assert p.vibe == "professional"


# ────────────────────────────────────────────────────────────────────
# Mixed samples — educational creator vibe
# ────────────────────────────────────────────────────────────────────

def test_educational_creator_with_some_emojis_is_mixed():
    p = classify_vibe(
        bio="copywriter ✍️ | i teach founders how to write emails that don't suck 📧",
        recent_captions=[
            (
                "3 mistakes I made in my first $100k year writing copy — and how to "
                "avoid them 👇 first, I priced by the hour instead of by outcome. "
                "second, I took every client who could pay rather than every client "
                "who fit. third, I skipped the discovery call thinking the brief "
                "would be enough."
            ),
            (
                "A framework I use for every sales email: hook → contrast → proof → "
                "ask. Most emails skip the contrast step which is exactly the part "
                "that makes people read past line 2."
            ),
            "saving this one for later 📌",
        ],
    )
    assert p.vibe in ("mixed", "professional"), f"got {p.vibe} ({p.reasoning})"
    # Educational creator should NOT be classified as casual
    assert p.vibe != "casual"


# ────────────────────────────────────────────────────────────────────
# Edge cases
# ────────────────────────────────────────────────────────────────────

def test_empty_bio_no_captions_returns_unknown():
    p = classify_vibe(bio="", recent_captions=[])
    assert p.vibe == "unknown"
    assert p.confidence < 0.3


def test_minimal_bio_only_returns_low_confidence():
    p = classify_vibe(bio="dad", recent_captions=[])
    # Either unknown or very low confidence
    assert p.vibe == "unknown" or p.confidence < 0.5


def test_emoji_only_caption_classifies_casual():
    p = classify_vibe(
        bio="✨🌊🔥",
        recent_captions=["🔥🔥🔥", "😍😍", "💯", "🚀"],
    )
    assert p.vibe == "casual"


# ────────────────────────────────────────────────────────────────────
# Signal extraction sanity
# ────────────────────────────────────────────────────────────────────

def test_signals_attached_to_profile():
    p = classify_vibe(
        bio="founder of ABC Inc.",
        recent_captions=["A thoughtful post about strategy.", "Another structured caption."],
    )
    assert "emoji_density" in p.signals
    assert "avg_caption_length" in p.signals
    assert "pro_keyword_hits" in p.signals
    assert p.signals["pro_keyword_hits"] >= 1   # "founder"


def test_profile_serialises():
    p = classify_vibe(bio="founder", recent_captions=["x"])
    d = p.to_payload()
    assert d["vibe"] in ("casual", "professional", "mixed", "unknown")
    assert "confidence" in d
    assert "signals" in d
    assert "suggested_comment_style" in d


# ────────────────────────────────────────────────────────────────────
# Comment template integrity
# ────────────────────────────────────────────────────────────────────

def test_all_vibes_have_templates():
    for vibe in ("casual", "professional", "mixed", "unknown"):
        t = template_for(vibe)  # type: ignore[arg-type]
        assert "style_label" in t
        assert "rules" in t
        assert "example_starters" in t
        assert len(t["example_starters"]) >= 1


def test_casual_template_has_emojis_in_starters():
    starters = COMMENT_TEMPLATES["casual"]["example_starters"]
    # At least 3 starters should contain emojis
    from app.modules.m5_warmup.vibe_classifier import EMOJI_RE
    emoji_starters = [s for s in starters if EMOJI_RE.search(s)]
    assert len(emoji_starters) >= 3, f"casual template missing emoji starters: {starters}"


def test_professional_template_starters_have_no_emojis():
    from app.modules.m5_warmup.vibe_classifier import EMOJI_RE
    starters = COMMENT_TEMPLATES["professional"]["example_starters"]
    for s in starters:
        assert not EMOJI_RE.search(s), f"professional starter has emoji: {s!r}"
