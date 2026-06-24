"""
M5 — Comment template library, keyed by vibe.

These are STARTING POINTS surfaced to the human operator in the Discord comment
queue, not auto-sent text. Mason's spec keeps comments human-in-the-loop —
the classifier picks the style, the human writes the actual comment.

Each entry includes:
  - style_label    : short tag the human sees ("fire emojis", "thoughtful one-liner")
  - example_starters: 3-5 concrete examples in that style
  - rules          : style rules to follow / avoid

The human can use a starter verbatim, adapt it, or ignore them entirely. The
templates are deliberately generic — personalization comes from the human
reading the specific post they're commenting on.
"""
from __future__ import annotations
from .vibe import Vibe


COMMENT_TEMPLATES: dict[Vibe, dict] = {
    "casual": {
        "style_label": "Fire emojis / hype reaction",
        "rules": [
            "lowercase, no punctuation needed",
            "1-3 emojis, prefer 🔥 🚀 💯 ⚡ 👏",
            "≤5 words ideally",
            "no questions — pure reaction",
            "match THEIR specific energy from the post itself",
        ],
        "example_starters": [
            "🔥🔥🔥",
            "this is sick",
            "absolute fire",
            "🚀🚀",
            "letsgoo",
            "💯",
            "underrated take",
        ],
    },
    "professional": {
        "style_label": "Thoughtful, substantive compliment",
        "rules": [
            "full sentence, proper capitalization",
            "zero emojis (or 1 max if the post itself uses one)",
            "reference the SPECIFIC idea/point they made",
            "10-20 words",
            "no compliments on appearance, only on substance",
            "no questions — those belong in DMs, not comments",
        ],
        "example_starters": [
            "The point about [SPECIFIC IDEA] hits hard — most people miss exactly that.",
            "Surprisingly few people actually do step 3 — appreciate the honesty here.",
            "This reframe is the part most courses skip over.",
            "Curious how this changes once [VARIABLE] shifts — but the principle holds.",
            "The bit about [SPECIFIC IDEA] is the half nobody talks about.",
        ],
    },
    "mixed": {
        "style_label": "Friendly + substantive",
        "rules": [
            "conversational tone, lowercase ok",
            "0-1 emojis (only if the post has one)",
            "react to ONE specific thing they said",
            "5-15 words",
            "feel like a friend reading the post, not a fan",
        ],
        "example_starters": [
            "the [SPECIFIC IDEA] part is so true",
            "this clicked — been thinking about exactly this",
            "love the framing of [SPECIFIC IDEA]",
            "underrated point about [SPECIFIC IDEA]",
            "this is the part i needed to hear today",
        ],
    },
    "unknown": {
        "style_label": "Default mixed style (review profile first)",
        "rules": [
            "vibe classifier had low confidence — review the profile and 2-3 recent posts before commenting",
            "if still unclear, default to the 'mixed' style above",
            "if profile looks bot-like or off-niche, skip the comment entirely and mark the warming action 'skipped'",
        ],
        "example_starters": [
            "(review profile first)",
        ],
    },
}


def template_for(vibe: Vibe) -> dict:
    """Get the template block for a vibe."""
    return COMMENT_TEMPLATES.get(vibe, COMMENT_TEMPLATES["unknown"])
