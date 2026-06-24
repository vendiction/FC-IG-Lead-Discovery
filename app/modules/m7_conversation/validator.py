"""
M7 — Output validator.

Every agent decision is run through this before any DM is sent. The validator
can FORCE A HANDOFF even if the agent confidently wanted to send.

Checks:
1. Length cap (Mason: "a sentence or two at most" — we cap at 280 chars,
   max 3 sentences, in-conversation).
2. Banned phrases (corporate boilerplate).
3. I/me vs you/your ratio (Mason: must use you/your more).
4. Confidence floor (env-tunable; default 0.6 for replies, 0.75 for advance).
5. Big-ask too early (no calendar/call asks in first 2 outbound messages).
6. High-value inbound detection — force handoff if the prospect is
   "emotionally in motion" (asking about process/cost/results).
7. Repeated unscripted objection — escalation handoff.
"""
from __future__ import annotations
import os
import re
from .decision import AgentDecision, ValidationResult, HandoffReason
from .prompts import (
    BANNED_PHRASES,
    VOICE_RULES,
    HIGH_VALUE_INBOUND_SIGNALS,
    OBJECTION_REFRAMES,
)


# ── Tunable thresholds (env-overridable) ────────────────────────────

CONFIDENCE_FLOOR_REPLY = float(os.getenv("M7_CONFIDENCE_FLOOR_REPLY", "0.60"))
CONFIDENCE_FLOOR_ADVANCE = float(os.getenv("M7_CONFIDENCE_FLOOR_ADVANCE", "0.75"))
BIG_ASK_GRACE_OUTBOUND_COUNT = int(os.getenv("M7_BIG_ASK_GRACE_COUNT", "2"))
MAX_OBJECTIONS_PER_TYPE = int(os.getenv("M7_MAX_OBJECTIONS_PER_TYPE", "1"))


# ── Regexes ──────────────────────────────────────────────────────────

# Big-ask patterns: calendar links, "30 min call", "hop on a call", etc.
BIG_ASK_PATTERNS = [
    re.compile(r"\bcalendly\.com\b", re.I),
    re.compile(r"\bcal\.com\b", re.I),
    re.compile(r"\bsavvycal\b", re.I),
    re.compile(r"\b30[\s-]?min(ute)?\s+(call|chat|meeting)\b", re.I),
    re.compile(r"\bhop on (a )?call\b", re.I),
    re.compile(r"\bbook (a )?(call|meeting|time)\b", re.I),
    re.compile(r"\bschedule (a )?call\b", re.I),
]

# "I/me" vs "you/your" tokenizer (simple — good enough)
I_ME_PATTERN = re.compile(r"\b(i|i'm|i'll|i've|i'd|me|my|mine|myself)\b", re.I)
YOU_YOUR_PATTERN = re.compile(r"\b(you|you're|you'll|you've|you'd|your|yours|yourself)\b", re.I)

SENTENCE_SPLIT = re.compile(r"[.!?]+(?:\s|$)")


# ────────────────────────────────────────────────────────────────────


def _count_sentences(text: str) -> int:
    parts = [p for p in SENTENCE_SPLIT.split(text.strip()) if p.strip()]
    return max(1, len(parts))


def _detect_high_value_inbound(last_inbound: str) -> bool:
    """Did the prospect just signal they're 'emotionally in motion'?"""
    text = last_inbound.lower()
    return any(sig in text for sig in HIGH_VALUE_INBOUND_SIGNALS)


def _is_verbatim_reframe(text: str) -> bool:
    """Allow verbatim Mason reframes to bypass the length cap."""
    if not text:
        return False
    text_norm = text.strip()
    for ref in OBJECTION_REFRAMES.values():
        if text_norm == ref["reframe_verbatim"].strip():
            return True
    return False


def validate(
    decision: AgentDecision,
    *,
    last_inbound: str,
    outbound_count_so_far: int,
    objections_handled_so_far: list[str],
) -> ValidationResult:
    """
    Run all checks. Returns a ValidationResult — caller acts on .final_action().

    Args:
        decision: raw agent output (already parsed via Pydantic)
        last_inbound: the prospect's most recent message text
        outbound_count_so_far: how many outbound messages WE'VE sent before this one
        objections_handled_so_far: list of objection labels already addressed
                                   (e.g., ['uncertainty']) — for repeat detection
    """
    violations: list[str] = []
    forced_handoff = False
    forced_reason: HandoffReason | None = None

    # ── 1. High-value inbound — strongest override ──────────────────
    # Even if the agent confidently wants to reply, if the prospect is
    # asking about process/cost/results, humans close better. Hand off.
    if _detect_high_value_inbound(last_inbound) and decision.action != "handoff":
        forced_handoff = True
        forced_reason = "high_value"
        violations.append("prospect signaled buy-intent (high-value inbound)")

    # ── 2. Confidence floor ─────────────────────────────────────────
    if decision.action == "reply" and decision.confidence < CONFIDENCE_FLOOR_REPLY:
        forced_handoff = True
        forced_reason = forced_reason or "low_confidence"
        violations.append(
            f"reply confidence {decision.confidence:.2f} < floor {CONFIDENCE_FLOOR_REPLY}"
        )
    elif decision.action == "advance_stage" and decision.confidence < CONFIDENCE_FLOOR_ADVANCE:
        forced_handoff = True
        forced_reason = forced_reason or "low_confidence"
        violations.append(
            f"advance confidence {decision.confidence:.2f} < floor {CONFIDENCE_FLOOR_ADVANCE}"
        )

    # ── 3. Repeated objection — reframe didn't land ─────────────────
    if (
        decision.detected_objection
        and decision.detected_objection in objections_handled_so_far
        and objections_handled_so_far.count(decision.detected_objection) >= MAX_OBJECTIONS_PER_TYPE
    ):
        forced_handoff = True
        forced_reason = forced_reason or "objection_escalation"
        violations.append(
            f"objection '{decision.detected_objection}' raised again after prior reframe"
        )

    # ── 4. Message-level checks (only if there IS an outbound) ──────
    msg = decision.next_message
    if msg:
        # Length cap — unless it's a verbatim Mason reframe (those are LONG and approved)
        if not _is_verbatim_reframe(msg):
            if len(msg) > VOICE_RULES["max_message_length_chars"]:
                violations.append(
                    f"message {len(msg)} chars > cap {VOICE_RULES['max_message_length_chars']}"
                )
                forced_handoff = True
                forced_reason = forced_reason or "low_confidence"

            n_sentences = _count_sentences(msg)
            if n_sentences > VOICE_RULES["max_sentences_per_message"]:
                violations.append(
                    f"message has {n_sentences} sentences > cap "
                    f"{VOICE_RULES['max_sentences_per_message']}"
                )
                forced_handoff = True
                forced_reason = forced_reason or "low_confidence"

        # Banned corporate phrases
        for banned in BANNED_PHRASES:
            if banned.lower() in msg.lower():
                violations.append(f"banned phrase: {banned!r}")
                forced_handoff = True
                forced_reason = forced_reason or "low_confidence"

        # Big-ask too early
        if outbound_count_so_far < BIG_ASK_GRACE_OUTBOUND_COUNT:
            for pat in BIG_ASK_PATTERNS:
                if pat.search(msg):
                    violations.append(
                        f"big-ask pattern in message {outbound_count_so_far + 1} "
                        f"(grace count: {BIG_ASK_GRACE_OUTBOUND_COUNT})"
                    )
                    forced_handoff = True
                    forced_reason = forced_reason or "low_confidence"
                    break

        # I/me vs you/your ratio
        i_count = len(I_ME_PATTERN.findall(msg))
        you_count = len(YOU_YOUR_PATTERN.findall(msg))
        if i_count + you_count > 0:
            i_ratio = i_count / (i_count + you_count)
            if i_ratio > VOICE_RULES["i_to_you_ratio_max"]:
                violations.append(
                    f"self-focus: I/me ratio {i_ratio:.2f} > max "
                    f"{VOICE_RULES['i_to_you_ratio_max']} (I={i_count}, you={you_count})"
                )
                # This one is a SOFT failure — flag but don't force handoff
                # unless combined with other issues. The agent might be
                # legitimately disclosing context.

    # ── 5. Handoff action with no reason — pin it to nuance_required ─
    if decision.action == "handoff" and not decision.handoff_reason:
        forced_reason = "nuance_required"

    return ValidationResult(
        decision=decision,
        is_valid=(not violations),
        forced_handoff=forced_handoff,
        forced_handoff_reason=forced_reason,
        violations=violations,
    )
