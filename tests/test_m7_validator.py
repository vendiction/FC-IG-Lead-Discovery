"""Tests for the M7 output validator."""
from __future__ import annotations
import pytest

from app.modules.m7_conversation.decision import AgentDecision
from app.modules.m7_conversation.validator import validate
from app.modules.m7_conversation.prompts import OBJECTION_REFRAMES


def _d(**kwargs) -> AgentDecision:
    defaults = dict(
        action="reply",
        next_message="cool, sounds good",
        confidence=0.85,
        reasoning="test",
    )
    defaults.update(kwargs)
    return AgentDecision(**defaults)


# ── Happy path ──────────────────────────────────────────────────────

def test_clean_reply_passes():
    r = validate(
        _d(next_message="totally — what's been the biggest blocker so far?"),
        last_inbound="just trying to grow my list",
        outbound_count_so_far=2,
        objections_handled_so_far=[],
    )
    assert r.is_valid
    assert not r.forced_handoff


# ── High-value inbound forces handoff ───────────────────────────────

def test_high_value_inbound_forces_handoff():
    r = validate(
        _d(),
        last_inbound="sounds great, how does it work? what does it cost?",
        outbound_count_so_far=2,
        objections_handled_so_far=[],
    )
    assert r.forced_handoff
    assert r.forced_handoff_reason == "high_value"


def test_call_request_forces_handoff():
    r = validate(
        _d(),
        last_inbound="can we hop on a quick call?",
        outbound_count_so_far=2,
        objections_handled_so_far=[],
    )
    assert r.forced_handoff
    assert r.forced_handoff_reason == "high_value"


# ── Confidence floor ────────────────────────────────────────────────

def test_low_confidence_reply_forces_handoff():
    r = validate(
        _d(confidence=0.4),
        last_inbound="cool",
        outbound_count_so_far=2,
        objections_handled_so_far=[],
    )
    assert r.forced_handoff
    assert r.forced_handoff_reason == "low_confidence"


def test_medium_confidence_advance_forces_handoff():
    # advance threshold is 0.75; reply threshold is 0.60
    r = validate(
        _d(action="advance_stage", target_stage="escalation",
           next_message="cool — mind if I share a quick thought?",
           confidence=0.65),
        last_inbound="sure",
        outbound_count_so_far=2,
        objections_handled_so_far=[],
    )
    assert r.forced_handoff
    assert r.forced_handoff_reason == "low_confidence"


# ── Banned phrases ──────────────────────────────────────────────────

def test_banned_phrase_forces_handoff():
    r = validate(
        _d(next_message="Dear Sir, I hope this message finds you well."),
        last_inbound="hi",
        outbound_count_so_far=2,
        objections_handled_so_far=[],
    )
    assert r.forced_handoff
    assert any("banned phrase" in v for v in r.violations)


# ── Big-ask too early ───────────────────────────────────────────────

def test_calendar_link_in_first_outbound_forces_handoff():
    r = validate(
        _d(next_message="cool, book here: calendly.com/jon/30min"),
        last_inbound="ok",
        outbound_count_so_far=0,
        objections_handled_so_far=[],
    )
    assert r.forced_handoff
    assert any("big-ask" in v for v in r.violations)


def test_call_ask_after_grace_period_ok():
    r = validate(
        _d(next_message="up for a quick 30 minute chat next week?"),
        last_inbound="that's interesting",
        outbound_count_so_far=4,   # well past grace
        objections_handled_so_far=[],
    )
    # Should NOT force handoff for big-ask; might still pass other checks
    assert "big-ask" not in " ".join(r.violations)


# ── Length cap ──────────────────────────────────────────────────────

def test_overlong_message_forces_handoff():
    long = "this is a really long message " * 20  # >280 chars
    r = validate(
        _d(next_message=long),
        last_inbound="ok",
        outbound_count_so_far=2,
        objections_handled_so_far=[],
    )
    assert r.forced_handoff
    assert any("chars >" in v for v in r.violations)


def test_verbatim_reframe_bypasses_length_cap():
    """Mason's verbatim reframes are LONG and approved."""
    reframe = OBJECTION_REFRAMES["uncertainty"]["reframe_verbatim"]
    r = validate(
        _d(next_message=reframe, detected_objection="uncertainty"),
        last_inbound="not sure this is for me",
        outbound_count_so_far=2,
        objections_handled_so_far=[],
    )
    # Length check should be bypassed; other checks should still pass
    assert not any("chars >" in v for v in r.violations)


# ── Repeated objection escalates ────────────────────────────────────

def test_repeated_objection_forces_handoff():
    r = validate(
        _d(detected_objection="uncertainty"),
        last_inbound="i still don't know",
        outbound_count_so_far=3,
        objections_handled_so_far=["uncertainty"],   # already addressed once
    )
    assert r.forced_handoff
    assert r.forced_handoff_reason == "objection_escalation"


# ── Self-focus is flagged as violation but not forced handoff ───────

def test_self_focus_flagged_softly():
    r = validate(
        _d(next_message="I think I should tell you about myself and my work that I do"),
        last_inbound="hi",
        outbound_count_so_far=2,
        objections_handled_so_far=[],
    )
    # Should flag self-focus
    assert any("self-focus" in v for v in r.violations)


# ── Sentence count cap ──────────────────────────────────────────────

def test_too_many_sentences_forces_handoff():
    msg = "one. two. three. four."   # 4 sentences > cap of 3
    r = validate(
        _d(next_message=msg),
        last_inbound="ok",
        outbound_count_so_far=2,
        objections_handled_so_far=[],
    )
    assert r.forced_handoff
    assert any("sentences >" in v for v in r.violations)
