"""
M6 Opener Generator — Validation + LLM dispatch.

LLM_MODE is read from settings or env:
- 'manual_paste': prints prompt to stdout, blocks on input() for human paste
- 'api': calls Anthropic directly

Validates output against Mason's hard rules. Returns None on failure (caller
can retry up to N times).

Bug fix (2026-06-23):
  generate_via_api was importing `from app.core.settings import settings`
  bare, which crashed if settings.py doesn't export that name. Both llm_mode()
  and generate_via_api() now resolve API key via try/except + env fallback,
  matching how llm_mode() already handled the same path. Also: validator now
  rejects raw technical gap labels (like "homepage_conversion") appearing in
  the output — defense-in-depth against the LLM ignoring the SYSTEM_PROMPT rule.
"""
from __future__ import annotations
import os
import re
import sys
from dataclasses import dataclass
from typing import Optional


# ────────────────────────────────────────────────────────────────────
# Hard validators (Mason's rules)
# ────────────────────────────────────────────────────────────────────

MAX_CHAR_COUNT = 160
MAX_FIRST_PERSON_PRONOUNS = 2
FIRST_PERSON_PRONOUNS = ["i ", " i'", " me ", " me.", " me,", " me?", " me!",
                        " my ", " mine ", " we ", " our ", " us "]

# Generic greetings Mason explicitly bans
BANNED_OPENINGS = [
    "hey,", "hi,", "hello,",
    "hope you're doing well",
    "hope you are doing well",
    "hope this finds you well",
    "i hope you",
    "just wanted to",
    "i'm reaching out",
    "i'd love to",
    "i help",
    "we help",
    "quick question for you about how we",
]

# Raw technical labels the LLM must NEVER include verbatim in the output.
# These come from the gap_analysis columns / primary_gap values. If any
# appear in the generated opener, the LLM ignored the prompt — fail it.
BANNED_RAW_LABELS = [
    "homepage_conversion",
    "lead_magnet_missing",
    "email_revenue_underperform",
    "content_struggle",
    "product_page_competitor",
    "local_seo",
    "cross_platform_mismatch",
    "personal_hook",
    "gap_hook",
]


@dataclass
class ValidationResult:
    valid: bool
    char_count: int
    uses_ellipsis: bool
    ends_with_question: bool
    fails: list[str]


def validate_opener(text: str, prior_openers: Optional[list[str]] = None) -> ValidationResult:
    """Check every Mason hard rule."""
    fails: list[str] = []
    t = text.strip()
    t_lower = t.lower()

    # 1. Character count
    char_count = len(t)
    if char_count > MAX_CHAR_COUNT:
        fails.append(f"too long: {char_count} chars > {MAX_CHAR_COUNT}")
    if char_count < 10:
        fails.append(f"too short: {char_count} chars")

    # 2. Must end with ... or ? (curiosity, not statement)
    uses_ellipsis = t.endswith("...") or t.endswith("…")
    ends_with_question = t.endswith("?")
    if not (uses_ellipsis or ends_with_question):
        fails.append("doesn't end with ... or ?")

    # 3. Banned generic openings
    for banned in BANNED_OPENINGS:
        if t_lower.startswith(banned):
            fails.append(f"banned opening: starts with '{banned}'")
            break

    # 4. First-person pronoun count
    fp_count = 0
    padded = f" {t_lower} "
    for pronoun in FIRST_PERSON_PRONOUNS:
        fp_count += padded.count(pronoun)
    if fp_count > MAX_FIRST_PERSON_PRONOUNS:
        fails.append(f"too many first-person pronouns: {fp_count} > {MAX_FIRST_PERSON_PRONOUNS}")

    # 5. Uniqueness vs prior openers to this prospect
    if prior_openers:
        for prior in prior_openers:
            if _too_similar(t, prior):
                fails.append(f"too similar to prior opener: {prior[:60]}")
                break

    # 6. No quoted output (Claude sometimes wraps in quotes)
    if t.startswith('"') and t.endswith('"'):
        fails.append("output is wrapped in quotes (should be raw text)")

    # 7. Raw technical label leak (the "homepage_conversion" bug)
    for label in BANNED_RAW_LABELS:
        if label in t_lower:
            fails.append(f"raw technical label leaked into output: '{label}'")
            break

    return ValidationResult(
        valid=len(fails) == 0,
        char_count=char_count,
        uses_ellipsis=uses_ellipsis,
        ends_with_question=ends_with_question,
        fails=fails,
    )


def _too_similar(a: str, b: str) -> bool:
    """Simple Jaccard on word sets. If ≥60% overlap, treat as duplicate."""
    wa = set(re.findall(r"\w+", a.lower()))
    wb = set(re.findall(r"\w+", b.lower()))
    if not wa or not wb:
        return False
    overlap = len(wa & wb) / max(len(wa | wb), 1)
    return overlap >= 0.60


# ────────────────────────────────────────────────────────────────────
# Settings/env helpers — robust to whatever shape app.core.settings has
# ────────────────────────────────────────────────────────────────────

def _settings_attr(name: str) -> Optional[str]:
    """Try to read an attr from app.core.settings.settings. Never raise."""
    try:
        from app.core.settings import settings  # type: ignore[import-not-found]
        val = getattr(settings, name, None)
        return val if val else None
    except Exception:
        return None


def _resolve_anthropic_api_key() -> str:
    """settings.anthropic_api_key, then ANTHROPIC_API_KEY env. Raise if neither."""
    key = _settings_attr("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not configured — checked app.core.settings.settings "
            "and ANTHROPIC_API_KEY env var, both missing"
        )
    return key


def _resolve_anthropic_model() -> str:
    """Allow env override of the model in case Anthropic ships a newer Sonnet."""
    return (
        _settings_attr("opener_model")
        or os.environ.get("M6_OPENER_MODEL")
        or "claude-sonnet-4-6"
    )


# ────────────────────────────────────────────────────────────────────
# LLM dispatch
# ────────────────────────────────────────────────────────────────────

def llm_mode() -> str:
    """Read LLM_MODE from settings; default to manual_paste."""
    return _settings_attr("llm_mode") or os.getenv("LLM_MODE", "manual_paste")


def generate_via_manual_paste(system: str, user: str, prospect_handle: str) -> str:
    """
    Print the full prompt to stdout. Block on input() until human pastes response.
    Designed for V1 testing with Claude Max — $0 LLM cost.
    """
    print("\n" + "=" * 70)
    print(f"  M6 OPENER GENERATION — manual_paste mode — prospect: @{prospect_handle}")
    print("=" * 70)
    print("\n--- SYSTEM PROMPT (paste into Claude) ---\n")
    print(system)
    print("\n--- USER PROMPT (paste into Claude) ---\n")
    print(user)
    print("\n" + "=" * 70)
    print("Paste Claude's response below. End with a line containing ONLY 'END' (then Enter):")
    print("=" * 70)
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "END":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def generate_via_api(system: str, user: str) -> tuple[str, dict]:
    """Call Anthropic API. Returns (text, raw_response_dict for logging)."""
    import anthropic

    api_key = _resolve_anthropic_api_key()
    model = _resolve_anthropic_model()
    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model,
        max_tokens=300,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in msg.content if hasattr(b, "text")).strip()
    raw = {
        "model": msg.model,
        "id": msg.id,
        "stop_reason": msg.stop_reason,
        "input_tokens": msg.usage.input_tokens,
        "output_tokens": msg.usage.output_tokens,
    }
    return text, raw


def generate(system: str, user: str, prospect_handle: str) -> tuple[str, dict]:
    """Main dispatcher. Returns (opener_text, metadata)."""
    mode = llm_mode()
    if mode == "api":
        text, raw = generate_via_api(system, user)
        return text, {"mode": "api", "raw": raw}
    else:
        text = generate_via_manual_paste(system, user, prospect_handle)
        return text, {"mode": "manual_paste", "model": "claude-max-via-human"}
