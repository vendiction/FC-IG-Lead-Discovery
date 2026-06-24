"""
M7 — Agent decision schema.

The agent returns a JSON object matching AgentDecision. The validator and
worker only ever read fields from this schema — never the raw LLM output.
"""
from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator


Stage = Literal[
    "opener", "escalation", "invitation", "action",
    "closed_won", "closed_lost", "ghosted", "handed_off",
]

Action = Literal["reply", "advance_stage", "handoff", "hold", "drop"]

DetectedObjection = Literal["uncertainty", "overwhelm", "hard_no", "other"]

HandoffReason = Literal[
    "high_value", "low_confidence", "nuance_required",
    "user_requested", "objection_escalation",
]


class AgentDecision(BaseModel):
    """What the agent returns for a single conversation turn."""

    action: Action
    next_message: Optional[str] = None
    target_stage: Optional[Stage] = None
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    detected_objection: Optional[DetectedObjection] = None
    micro_commitment_obtained: Optional[str] = None
    handoff_reason: Optional[HandoffReason] = None

    @field_validator("next_message")
    @classmethod
    def _strip_message(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return v.strip()

    def is_send(self) -> bool:
        """True if this decision results in an outbound DM."""
        return self.action in ("reply", "advance_stage") and bool(self.next_message)

    def is_terminal(self) -> bool:
        """True if this decision ends the conversation (no more turns)."""
        return (
            self.action == "drop"
            or self.target_stage in ("closed_won", "closed_lost", "handed_off")
        )


class ValidationResult(BaseModel):
    """Output of the validator — what M7 actually acts on."""

    decision: AgentDecision
    is_valid: bool
    forced_handoff: bool = False
    forced_handoff_reason: Optional[HandoffReason] = None
    violations: list[str] = Field(default_factory=list)

    def final_action(self) -> Action:
        """The action after validator overrides."""
        if self.forced_handoff:
            return "handoff"
        return self.decision.action
