"""Pure-logic tests for the Selling Map state machine."""
from __future__ import annotations
import pytest

from app.modules.m7_conversation.decision import AgentDecision
from app.modules.m7_conversation.selling_map import (
    next_stage,
    is_terminal,
    IllegalTransition,
    PROGRESSION,
    TERMINAL_STAGES,
)


def _decide(action: str, **kwargs) -> AgentDecision:
    defaults = dict(
        action=action,
        confidence=0.8,
        reasoning="test",
    )
    defaults.update(kwargs)
    return AgentDecision(**defaults)


# ── Reply / Hold: stay in same stage ────────────────────────────────

@pytest.mark.parametrize("stage", PROGRESSION)
def test_reply_keeps_stage(stage):
    d = _decide("reply", next_message="hey")
    assert next_stage(stage, d) == stage


@pytest.mark.parametrize("stage", PROGRESSION)
def test_hold_keeps_stage(stage):
    d = _decide("hold")
    assert next_stage(stage, d) == stage


# ── Linear progression: must advance one step at a time ─────────────

def test_advance_opener_to_escalation():
    d = _decide("advance_stage", next_message="cool", target_stage="escalation")
    assert next_stage("opener", d) == "escalation"


def test_advance_escalation_to_invitation():
    d = _decide("advance_stage", next_message="mind if I send something?",
                target_stage="invitation")
    assert next_stage("escalation", d) == "invitation"


def test_advance_invitation_to_action():
    d = _decide("advance_stage", next_message="here's the link", target_stage="action")
    assert next_stage("invitation", d) == "action"


def test_cannot_skip_stages():
    d = _decide("advance_stage", next_message="here's the link", target_stage="invitation")
    with pytest.raises(IllegalTransition, match="cannot skip stages"):
        next_stage("opener", d)


def test_cannot_go_backwards():
    d = _decide("advance_stage", next_message="back up", target_stage="opener")
    with pytest.raises(IllegalTransition, match="cannot skip stages"):
        next_stage("escalation", d)


# ── Terminal transitions ────────────────────────────────────────────

def test_handoff_action_lands_in_handed_off():
    d = _decide("handoff", handoff_reason="nuance_required")
    assert next_stage("escalation", d) == "handed_off"


def test_drop_action_lands_in_closed_lost():
    d = _decide("drop")
    assert next_stage("escalation", d) == "closed_lost"


def test_action_stage_can_close_won():
    d = _decide("advance_stage", next_message="🎉", target_stage="closed_won")
    assert next_stage("action", d) == "closed_won"


# ── Guards ──────────────────────────────────────────────────────────

def test_terminal_stages_reject_further_transitions():
    d = _decide("reply", next_message="?")
    for term in TERMINAL_STAGES:
        with pytest.raises(IllegalTransition, match="already terminal"):
            next_stage(term, d)


def test_advance_without_target_stage_fails():
    d = _decide("advance_stage", next_message="hey")
    with pytest.raises(IllegalTransition, match="requires target_stage"):
        next_stage("opener", d)


# ── is_terminal helper ──────────────────────────────────────────────

def test_is_terminal():
    assert is_terminal("closed_won")
    assert is_terminal("handed_off")
    assert not is_terminal("opener")
    assert not is_terminal("escalation")
