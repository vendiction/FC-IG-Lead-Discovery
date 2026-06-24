"""
M5 — Vibe classification types.

Mason's spec (task 2): comment with "fire emojis or professional compliments
depending on the target." The classifier produces a VibeProfile per prospect
which downstream code uses to pick the right comment template.

Categories:
  casual        — emoji-heavy, short captions, energetic/lifestyle/influencer vibe
                  → respond with emoji reactions or short hype
  professional  — structured prose, business keywords, low emoji density
                  → respond with substantive professional compliment
  mixed         — educational creator vibe, some emojis + structure
                  → friendly, light, references content specifically
  unknown       — not enough signal to classify; default to mixed templates,
                  flag the warming action for human review
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal


Vibe = Literal["casual", "professional", "mixed", "unknown"]


@dataclass
class VibeProfile:
    """The full output of the classifier — attached to warming_actions.human_payload."""

    vibe: Vibe
    confidence: float                            # 0.0–1.0
    method: Literal["heuristic", "llm", "fallback"]
    signals: dict[str, float | int | bool] = field(default_factory=dict)
    reasoning: str = ""
    suggested_comment_style: str = ""           # short hint for human queue

    def to_payload(self) -> dict:
        """Serialise for storing in warming_actions.human_payload JSONB."""
        return {
            "vibe": self.vibe,
            "confidence": round(self.confidence, 3),
            "method": self.method,
            "signals": self.signals,
            "reasoning": self.reasoning,
            "suggested_comment_style": self.suggested_comment_style,
        }
