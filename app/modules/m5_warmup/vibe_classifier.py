"""
M5 — Vibe classifier.

Two-phase design:
  Phase 1 (heuristic, ~free): regex + counting signals on bio + recent captions.
    Returns a confident classification for ~70-80% of prospects.
  Phase 2 (LLM, optional): for ambiguous cases (heuristic confidence < threshold),
    falls back to Claude Haiku — fast, cheap (~$0.001/prospect).

The LLM fallback is gated by M5_VIBE_USE_LLM=true env var. Default OFF —
the heuristic alone is good enough for V1 and saves cost on volume.

Usage:
    from app.modules.m5_warmup.vibe_classifier import classify_vibe
    profile = classify_vibe(
        bio=account["bio"],
        recent_captions=[...latest 5-10 caption texts...],
        follower_count=account["follower_count"],
    )
    # profile.vibe ∈ {"casual","professional","mixed","unknown"}
    # profile.to_payload() → stash in warming_actions.human_payload
"""
from __future__ import annotations
import os
import re
import statistics
from typing import Optional
import structlog

from .vibe import Vibe, VibeProfile

log = structlog.get_logger("m5.vibe")


# ────────────────────────────────────────────────────────────────────
# Tunables
# ────────────────────────────────────────────────────────────────────

# Heuristic confidence below this triggers the LLM fallback (if enabled)
LLM_FALLBACK_THRESHOLD = float(os.getenv("M5_VIBE_LLM_FALLBACK_THRESHOLD", "0.60"))
USE_LLM_FALLBACK = os.getenv("M5_VIBE_USE_LLM", "false").lower() == "true"
LLM_MODEL = os.getenv("M5_VIBE_MODEL", "claude-haiku-4-5-20251001")

# Signal thresholds tuned from copywriting + social media observation
CASUAL_EMOJI_DENSITY = 0.04       # 4+ emojis per 100 chars → casual
PRO_EMOJI_DENSITY = 0.005          # ≤0.5 emojis per 100 chars → professional
SHORT_CAPTION_CUTOFF = 80          # chars; below = "short caption" signal
LONG_CAPTION_CUTOFF = 300          # chars; above = "long caption" signal


# ────────────────────────────────────────────────────────────────────
# Emoji + token regexes
# ────────────────────────────────────────────────────────────────────

# Covers most common emoji ranges (BMP + supplementary planes used in social).
# Not exhaustive but catches >99% of what shows up in IG bios/captions.
EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F9FF"   # symbols & pictographs, supplemental
    "\U0001FA70-\U0001FAFF"   # extended-A
    "\U00002600-\U000027BF"   # misc + dingbats
    "\U0001F600-\U0001F64F"   # emoticons
    "\U0001F680-\U0001F6FF"   # transport
    "\U0001F900-\U0001F9FF"   # supplemental symbols
    "]",
    flags=re.UNICODE,
)

HASHTAG_RE = re.compile(r"#\w+")
ALLCAPS_WORD_RE = re.compile(r"\b[A-Z]{3,}\b")
URL_RE = re.compile(r"https?://\S+")

# Reuse the business keyword set the M4 scorer uses — single source of truth
# would be ideal but we redefine here to avoid a hard import cycle.
PROFESSIONAL_KEYWORDS = {
    "ceo", "founder", "co-founder", "cofounder", "owner", "entrepreneur",
    "consultant", "strategist", "advisor", "executive", "director",
    "phd", "md", "esq", "cpa",
    "agency", "firm", "studio", "consultancy",
    "speaker", "author of", "keynote",
}

CASUAL_KEYWORDS = {
    "vibes", "vibe", "energy", "lit", "fire", "lowkey", "highkey",
    "literally", "queen", "king", "bestie", "yall", "y'all",
    "deadass", "fr", "ngl", "tbh", "lol", "lmao", "omg",
    "let's gooo", "lets go", "ily", "iykyk",
}

EDUCATIONAL_MARKERS = {
    "how to", "tips", "lessons", "mistakes", "thread", "guide",
    "framework", "strategy", "principles", "rules", "system",
}


# ────────────────────────────────────────────────────────────────────
# Signal extraction
# ────────────────────────────────────────────────────────────────────


def _count_keyword_hits(text_lower: str, keywords: set[str]) -> int:
    """
    Count keyword occurrences with word-boundary matching.

    Substring matching breaks badly on short tokens — "lit" hits inside
    "quality", "king" hits inside "working", "fr" hits inside "from". Word
    boundaries are mandatory.

    Multi-word keywords ("co-founder", "let's go") are matched as phrases
    with boundaries at the ends only (apostrophes and hyphens treated as
    part of the token).
    """
    hits = 0
    for kw in keywords:
        kw_lower = kw.lower().strip()
        if not kw_lower:
            continue
        # Build pattern: \b at start and end, but allow internal chars
        # like apostrophes, hyphens, spaces
        pattern = r"(?<![a-z0-9])" + re.escape(kw_lower) + r"(?![a-z0-9])"
        if re.search(pattern, text_lower):
            hits += 1
    return hits


def _extract_signals(bio: str, captions: list[str]) -> dict:
    """Pull all heuristic signals from raw text."""
    bio = bio or ""
    captions = [c for c in (captions or []) if c]
    combined = bio + "\n" + "\n".join(captions)
    combined_lower = combined.lower()

    char_count = max(1, len(combined))
    emoji_count = len(EMOJI_RE.findall(combined))
    hashtag_count = len(HASHTAG_RE.findall(combined))
    allcaps_count = len(ALLCAPS_WORD_RE.findall(combined))

    caption_lengths = [len(c) for c in captions]
    avg_caption_len = statistics.mean(caption_lengths) if caption_lengths else 0.0
    median_caption_len = statistics.median(caption_lengths) if caption_lengths else 0.0

    pro_kw_hits = _count_keyword_hits(combined_lower, PROFESSIONAL_KEYWORDS)
    casual_kw_hits = _count_keyword_hits(combined_lower, CASUAL_KEYWORDS)
    edu_kw_hits = _count_keyword_hits(combined_lower, EDUCATIONAL_MARKERS)

    # Detect caption shape — emoji-only/short reactions vs structured prose
    emoji_dominant_captions = sum(
        1 for c in captions
        if c and len(EMOJI_RE.findall(c)) > 0
        and len(EMOJI_RE.findall(c)) / max(1, len(c)) > 0.15
    )
    long_prose_captions = sum(1 for c in captions if len(c) > LONG_CAPTION_CUTOFF)

    return {
        "char_count": char_count,
        "caption_count": len(captions),
        "emoji_count": emoji_count,
        "emoji_density": emoji_count / char_count,
        "hashtag_count": hashtag_count,
        "allcaps_word_count": allcaps_count,
        "avg_caption_length": round(avg_caption_len, 1),
        "median_caption_length": round(median_caption_len, 1),
        "pro_keyword_hits": pro_kw_hits,
        "casual_keyword_hits": casual_kw_hits,
        "edu_keyword_hits": edu_kw_hits,
        "emoji_dominant_captions": emoji_dominant_captions,
        "long_prose_captions": long_prose_captions,
    }


# ────────────────────────────────────────────────────────────────────
# Phase 1: heuristic classifier
# ────────────────────────────────────────────────────────────────────


def _classify_heuristic(signals: dict) -> tuple[Vibe, float, str]:
    """
    Return (vibe, confidence, reasoning) from signals alone.

    Confidence calibration:
      - 0.85+ : clear signal in one direction
      - 0.65–0.85 : leans but not dominant
      - <0.65 : ambiguous, LLM fallback recommended
    """
    if signals["caption_count"] == 0 and signals["char_count"] < 20:
        return ("unknown", 0.1, "almost no text available")

    casual_score = 0.0
    pro_score = 0.0
    mixed_score = 0.0

    # — Emoji density —
    ed = signals["emoji_density"]
    if ed >= CASUAL_EMOJI_DENSITY:
        casual_score += 2.0
    elif ed <= PRO_EMOJI_DENSITY:
        pro_score += 1.5
    else:
        mixed_score += 1.0

    # — Caption length pattern —
    avg_len = signals["avg_caption_length"]
    if avg_len > 0:
        if avg_len < SHORT_CAPTION_CUTOFF:
            casual_score += 1.0
        elif avg_len > LONG_CAPTION_CUTOFF:
            pro_score += 1.5
        else:
            mixed_score += 1.0

    # — Keyword hits —
    casual_score += min(2.0, signals["casual_keyword_hits"] * 0.7)
    pro_score += min(2.0, signals["pro_keyword_hits"] * 0.7)
    mixed_score += min(1.5, signals["edu_keyword_hits"] * 0.5)

    # — Caption shape —
    if signals["emoji_dominant_captions"] >= 2:
        casual_score += 1.5
    if signals["long_prose_captions"] >= 2:
        pro_score += 1.0

    # — All-caps shouting → casual signal (HYPE / LFG / etc.) —
    if signals["allcaps_word_count"] >= 2:
        casual_score += 0.5

    # — Hashtag density: high (>5) = casual/influencer, low (0-1) = pro —
    if signals["hashtag_count"] >= 5:
        casual_score += 0.5
    elif signals["hashtag_count"] == 0 and signals["caption_count"] >= 3:
        pro_score += 0.3

    total = casual_score + pro_score + mixed_score
    if total == 0:
        return ("unknown", 0.2, "no scoring signals fired")

    scores = {
        "casual": casual_score,
        "professional": pro_score,
        "mixed": mixed_score,
    }
    winner = max(scores, key=scores.get)
    winner_score = scores[winner]

    # Confidence = winner's share of total, scaled
    share = winner_score / total
    # Stretch into a meaningful range: 0.40 share → 0.50 conf, 1.0 share → 0.95 conf
    confidence = max(0.0, min(0.95, 0.20 + share * 0.75))

    reasoning = (
        f"casual={casual_score:.1f} pro={pro_score:.1f} mixed={mixed_score:.1f} "
        f"(emoji_density={ed:.3f}, avg_len={avg_len:.0f}, "
        f"casual_kw={signals['casual_keyword_hits']}, "
        f"pro_kw={signals['pro_keyword_hits']}, "
        f"edu_kw={signals['edu_keyword_hits']})"
    )

    # Resolve "mixed wins by tiny margin against another" → pick the runner-up
    # if it's clearly directional, otherwise keep mixed.
    if winner == "mixed":
        runner_up = max(["casual", "professional"], key=lambda k: scores[k])
        if scores[runner_up] > 0.7 * scores["mixed"]:
            # Mixed is real — leave it, but lower confidence
            confidence = min(confidence, 0.7)

    return (winner, confidence, reasoning)


# ────────────────────────────────────────────────────────────────────
# Phase 2: LLM fallback (opt-in)
# ────────────────────────────────────────────────────────────────────


def _classify_llm(bio: str, captions: list[str]) -> tuple[Vibe, float, str]:
    """Cheap Haiku call for ambiguous cases. Returns ('unknown', 0.0, ...) on any error."""
    try:
        from anthropic import Anthropic
    except ImportError:
        return ("unknown", 0.0, "anthropic SDK not available")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return ("unknown", 0.0, "no ANTHROPIC_API_KEY set")

    sample_captions = "\n---\n".join((captions or [])[:6])
    prompt = f"""Classify this Instagram profile's vibe for a sales outreach system.

BIO:
{bio[:500] or '(none)'}

RECENT CAPTIONS:
{sample_captions[:2000] or '(none)'}

Pick ONE label that describes how this prospect communicates online:
- "casual": emoji-heavy, short, energetic, lifestyle/influencer energy
- "professional": structured prose, business-focused, low emoji
- "mixed": educational creator — uses some structure AND some emojis
- "unknown": not enough signal

Return ONLY this JSON (no markdown, no preamble):
{{"vibe":"casual|professional|mixed|unknown","confidence":0.0-1.0,"reasoning":"one short sentence"}}"""

    try:
        client = Anthropic()
        resp = client.messages.create(
            model=LLM_MODEL,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        import json, re as _re
        match = _re.search(r"\{[\s\S]*\}", raw)
        if not match:
            return ("unknown", 0.0, f"llm returned no JSON: {raw[:100]!r}")
        data = json.loads(match.group(0))
        vibe = data.get("vibe", "unknown")
        if vibe not in ("casual", "professional", "mixed", "unknown"):
            vibe = "unknown"
        return (vibe, float(data.get("confidence", 0.5)), data.get("reasoning", "")[:200])
    except Exception as e:
        log.warning("m5.vibe.llm_failed", err=str(e))
        return ("unknown", 0.0, f"llm error: {e}")


# ────────────────────────────────────────────────────────────────────
# Public entrypoint
# ────────────────────────────────────────────────────────────────────


# Short hint strings shown to the human in the Discord comment queue,
# one per vibe. Vibe → suggested comment style guidance.
COMMENT_STYLE_HINTS = {
    "casual": "match their energy — fire emojis (🔥/🚀/💯), short reaction, lowercase",
    "professional": (
        "thoughtful one-liner referencing the specific value in their post — "
        "no emojis, full sentence"
    ),
    "mixed": (
        "friendly but substantive — quick reaction to a specific point in their "
        "caption, max 1 emoji, conversational tone"
    ),
    "unknown": (
        "not enough signal — default to mixed style and consider reviewing the "
        "profile manually before commenting"
    ),
}


def classify_vibe(
    *,
    bio: Optional[str] = None,
    recent_captions: Optional[list[str]] = None,
    follower_count: Optional[int] = None,
) -> VibeProfile:
    """
    Main entrypoint. Cheap by default (heuristic only). Set M5_VIBE_USE_LLM=true
    to enable the Haiku fallback for ambiguous cases.
    """
    bio = bio or ""
    captions = recent_captions or []

    signals = _extract_signals(bio, captions)
    if follower_count is not None:
        signals["follower_count"] = follower_count

    vibe, conf, reasoning = _classify_heuristic(signals)
    method: str = "heuristic"

    if conf < LLM_FALLBACK_THRESHOLD and USE_LLM_FALLBACK:
        log.info("m5.vibe.llm_fallback_triggered", heuristic_conf=conf, vibe=vibe)
        llm_vibe, llm_conf, llm_reason = _classify_llm(bio, captions)
        if llm_conf > conf:
            vibe = llm_vibe
            conf = llm_conf
            reasoning = f"[llm] {llm_reason} (heuristic: {reasoning})"
            method = "llm"

    if conf < 0.3:
        method = "fallback"
        vibe = "unknown"

    return VibeProfile(
        vibe=vibe,
        confidence=conf,
        method=method,   # type: ignore[arg-type]
        signals=signals,
        reasoning=reasoning,
        suggested_comment_style=COMMENT_STYLE_HINTS[vibe],
    )
