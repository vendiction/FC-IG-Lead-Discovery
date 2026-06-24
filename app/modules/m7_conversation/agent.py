"""
M7 — Claude agent wrapper.

Single responsibility: given conversation context, return a parsed AgentDecision.

Uses claude-sonnet-4-5 by default (env-overridable). The full system prompt
embeds Mason's verbatim Selling Map, objection reframes, anti-patterns, and
handoff rules — see prompts.py.
"""
from __future__ import annotations
import json
import os
import re
from typing import Optional
import structlog
from anthropic import Anthropic
from pydantic import ValidationError

from .decision import AgentDecision
from .prompts import build_system_prompt

log = structlog.get_logger("m7.agent")


MODEL = os.getenv("M7_AGENT_MODEL", "claude-sonnet-4-5")
MAX_TOKENS = int(os.getenv("M7_AGENT_MAX_TOKENS", "1024"))
TEMPERATURE = float(os.getenv("M7_AGENT_TEMPERATURE", "0.7"))


# Strips ```json fences and any leading prose, isolates the JSON object
_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}")


class AgentError(RuntimeError):
    """Raised when the LLM returns something we can't parse into AgentDecision."""


def _client() -> Anthropic:
    return Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def _format_history(messages: list[dict]) -> str:
    """Render the conversation log for the prompt."""
    if not messages:
        return "(no prior messages — this is the first inbound after our opener)"
    lines = []
    for m in messages:
        direction = m["direction"]
        body = m["body"]
        stage = m.get("stage_at_time") or "?"
        speaker = "AGENT" if direction == "outbound" else "PROSPECT"
        lines.append(f"[{stage}] {speaker}: {body}")
    return "\n".join(lines)


def _extract_json(raw: str) -> dict:
    """
    Pull the first JSON object out of the LLM response.

    Handles:
    - raw JSON
    - ```json ... ``` fenced
    - JSON with a leading sentence ("Here's my decision: { ... }")
    """
    raw = raw.strip()
    # Strip code fences
    for fence in ("```json", "```"):
        if raw.startswith(fence):
            raw = raw[len(fence):].lstrip()
            if raw.endswith("```"):
                raw = raw[:-3].rstrip()
    match = _JSON_BLOCK_RE.search(raw)
    if not match:
        raise AgentError(f"no JSON object found in response: {raw[:200]!r}")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise AgentError(f"JSON parse failed: {e}; raw={match.group(0)[:200]!r}") from e


def decide(
    *,
    stage: str,
    prospect_handle: str,
    prospect_primary_gap: Optional[str],
    prospect_xplat_source: Optional[str],
    prospect_total_score: int,
    prospect_is_high_value: bool,
    history: list[dict],
    last_inbound: str,
) -> AgentDecision:
    """
    Run one agent turn. Returns a validated AgentDecision.

    Raises AgentError if the LLM output cannot be parsed into the schema.
    """
    system_prompt = build_system_prompt(
        stage_name=stage,
        prospect_handle=prospect_handle,
        prospect_primary_gap=prospect_primary_gap,
        prospect_xplat_source=prospect_xplat_source,
        prospect_total_score=prospect_total_score,
        prospect_is_high_value=prospect_is_high_value,
        conversation_history=_format_history(history),
        last_inbound=last_inbound,
    )

    log.info(
        "m7.agent.invoke",
        prospect=prospect_handle,
        stage=stage,
        history_len=len(history),
        model=MODEL,
    )

    client = _client()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        system=system_prompt,
        messages=[
            {
                "role": "user",
                "content": (
                    "Decide the next action for this conversation turn. "
                    "Return ONLY the JSON object — no markdown, no preamble."
                ),
            }
        ],
    )

    # Concatenate all text blocks
    raw_text = "".join(
        block.text for block in resp.content if getattr(block, "type", None) == "text"
    )

    log.info(
        "m7.agent.response",
        prospect=prospect_handle,
        stop_reason=resp.stop_reason,
        usage_in=resp.usage.input_tokens,
        usage_out=resp.usage.output_tokens,
    )

    payload = _extract_json(raw_text)

    try:
        decision = AgentDecision(**payload)
    except ValidationError as e:
        raise AgentError(f"schema validation failed: {e}; payload={payload}") from e

    log.info(
        "m7.agent.decision",
        prospect=prospect_handle,
        action=decision.action,
        confidence=decision.confidence,
        objection=decision.detected_objection,
        handoff_reason=decision.handoff_reason,
    )
    return decision
