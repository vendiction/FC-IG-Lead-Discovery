"""
M7 — Selling Map state machine.

Pure functions: given a current stage and an agent decision, return the new
stage. No I/O, no DB — easy to unit test.

Mason's 4 progression stages are linear (opener → escalation → invitation →
action). Terminal stages: closed_won, closed_lost, ghosted, handed_off.
"""
from __future__ import annotations
from typing import Optional
from .decision import AgentDecision, Stage


# Linear progression order — agent can only advance forward by one step
# at a time. Skipping stages is not allowed (and would be rejected here).
PROGRESSION = ["opener", "escalation", "invitation", "action"]

TERMINAL_STAGES = {"closed_won", "closed_lost", "ghosted", "handed_off"}


class IllegalTransition(ValueError):
    """Raised when an agent decision proposes an invalid stage transition."""


def next_stage(current: Stage, decision: AgentDecision) -> Stage:
    """
    Compute the next stage given the current stage and the agent's decision.

    Rules:
    - If already terminal, no transition allowed.
    - If decision.action == 'handoff', go to 'handed_off'.
    - If decision.action == 'drop', go to 'closed_lost'.
    - If decision.action == 'advance_stage', target_stage must be the next
      stage in PROGRESSION (no skipping) OR a terminal stage.
    - If decision.action == 'reply' or 'hold', stay in current stage.
    """
    if current in TERMINAL_STAGES:
        raise IllegalTransition(f"already terminal: {current}")

    if decision.action == "handoff":
        return "handed_off"

    if decision.action == "drop":
        return "closed_lost"

    if decision.action in ("reply", "hold"):
        return current

    if decision.action == "advance_stage":
        target = decision.target_stage
        if target is None:
            raise IllegalTransition("advance_stage requires target_stage")
        # Terminal advance (e.g., action → closed_won) is allowed
        if target in TERMINAL_STAGES:
            return target
        # Otherwise must be the immediate next step in linear progression
        try:
            cur_idx = PROGRESSION.index(current)
        except ValueError:
            raise IllegalTransition(f"current stage {current} not in progression")
        try:
            tgt_idx = PROGRESSION.index(target)
        except ValueError:
            raise IllegalTransition(f"target stage {target} not in progression")
        if tgt_idx != cur_idx + 1:
            raise IllegalTransition(
                f"cannot skip stages: {current} → {target} "
                f"(only {PROGRESSION[cur_idx + 1] if cur_idx + 1 < len(PROGRESSION) else 'terminal'} allowed)"
            )
        return target

    raise IllegalTransition(f"unknown action: {decision.action}")


def is_terminal(stage: Stage) -> bool:
    return stage in TERMINAL_STAGES


def stage_for_handoff(reason: Optional[str]) -> Stage:
    """All handoff reasons → handed_off (the human takes over from there)."""
    return "handed_off"
