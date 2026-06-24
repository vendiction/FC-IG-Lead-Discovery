"""
M7 — Conversation worker main loop.

Per cycle, for each active IG account:
  1. poll_inbox() — get unread threads
  2. For each unread inbound from a prospect we already opened with:
       a. Record the inbound message
       b. Run the agent (Claude) → AgentDecision
       c. Validate (length, banned phrases, big-ask, high-value, confidence)
       d. If validator passed → send DM, update stage, log outbound
       e. If validator forced handoff → create handoff row, do NOT send
  3. Schedule ghost followups for conversations with no inbound > 48h

This worker REPLACES the conversation_worker.py stub created at M0.

Run as:
    python -m app.modules.m7_conversation.worker
"""
from __future__ import annotations
import asyncio
import os
from datetime import datetime, timezone, timedelta
from typing import Optional
import structlog

from app.core.logging import configure_logging  # type: ignore
from .agent import decide, AgentError
from .decision import AgentDecision
from .validator import validate
from .selling_map import next_stage, IllegalTransition
from .io_dm import poll_inbox, send_dm, can_send_dm_today
from . import repository as repo
from .prompts import (
    GHOST_FOLLOWUP_TEMPLATES,
    GHOST_FOLLOWUP_CADENCE_HOURS,
)

configure_logging()
log = structlog.get_logger("m7.worker")


POLL_INTERVAL_SECONDS = int(os.getenv("M7_POLL_INTERVAL_SECONDS", "300"))   # 5 min
BETWEEN_ACCOUNTS_SECONDS = int(os.getenv("M7_BETWEEN_ACCOUNTS_SECONDS", "30"))


# ────────────────────────────────────────────────────────────────────
# Per-conversation turn
# ────────────────────────────────────────────────────────────────────

async def _handle_inbound(
    conversation: dict,
    prospect: dict,
    inbound_text: str,
    ig_account: str,
) -> None:
    """Run one full agent turn for a single new inbound message."""
    conv_id = conversation["id"]
    stage = conversation["current_stage"]

    # 1. Record inbound
    repo.insert_inbound_message(conv_id, inbound_text, stage_at_time=stage)
    repo.touch_conversation_inbound(conv_id)
    cancelled = repo.cancel_followups_for_conversation(conv_id)
    if cancelled:
        log.info("m7.worker.followups_cancelled", conversation_id=conv_id, count=cancelled)

    # 2. Build agent context
    history = repo.list_messages(conv_id)
    # Exclude the inbound we just inserted from the formatted history,
    # since it's passed separately as last_inbound
    history_for_agent = [m for m in history if m["body"] != inbound_text or m["direction"] != "inbound"]

    objections_handled = conversation.get("objections_handled") or []

    # 3. Run the agent
    try:
        decision: AgentDecision = decide(
            stage=stage,
            prospect_handle=prospect["handle"],
            prospect_primary_gap=(prospect.get("primary_gaps") or [None])[0]
                if isinstance(prospect.get("primary_gaps"), list) else None,
            prospect_xplat_source=prospect.get("cross_platform_discovery_source"),
            prospect_total_score=prospect.get("total_score") or 0,
            prospect_is_high_value=prospect.get("is_high_value") or False,
            history=history_for_agent,
            last_inbound=inbound_text,
        )
    except AgentError as e:
        log.error("m7.worker.agent_failed", conversation_id=conv_id, err=str(e))
        # Force a handoff on unparseable LLM output
        repo.create_handoff(
            conversation_id=conv_id,
            prospect_id=prospect["id"],
            trigger_reason="low_confidence",
            trigger_detail=f"agent error: {e}",
            conversation_snapshot=history,
            ai_recommended_action=None,
        )
        repo.update_conversation_stage(conv_id, "handed_off", human_intervention=True)
        return

    # 4. Validate
    outbound_count = repo.count_outbound_messages(conv_id)
    result = validate(
        decision,
        last_inbound=inbound_text,
        outbound_count_so_far=outbound_count,
        objections_handled_so_far=objections_handled,
    )

    # 5. Forced handoff — log it and stop
    if result.forced_handoff:
        log.info(
            "m7.worker.forced_handoff",
            conversation_id=conv_id,
            reason=result.forced_handoff_reason,
            violations=result.violations,
        )
        repo.create_handoff(
            conversation_id=conv_id,
            prospect_id=prospect["id"],
            trigger_reason=result.forced_handoff_reason or "low_confidence",
            trigger_detail=" | ".join(result.violations) or "validator forced handoff",
            conversation_snapshot=history,
            ai_recommended_action=decision.next_message,
        )
        repo.update_conversation_stage(
            conv_id, "handed_off",
            objections_handled=_with_objection(objections_handled, decision.detected_objection),
            human_intervention=True,
        )
        return

    # 6. Agent voluntarily handed off (no validator override)
    if decision.action == "handoff":
        log.info("m7.worker.agent_handoff", conversation_id=conv_id,
                 reason=decision.handoff_reason)
        repo.create_handoff(
            conversation_id=conv_id,
            prospect_id=prospect["id"],
            trigger_reason=decision.handoff_reason or "nuance_required",
            trigger_detail=decision.reasoning,
            conversation_snapshot=history,
            ai_recommended_action=decision.next_message,
        )
        repo.update_conversation_stage(conv_id, "handed_off", human_intervention=True)
        return

    # 7. Drop
    if decision.action == "drop":
        log.info("m7.worker.drop", conversation_id=conv_id, reasoning=decision.reasoning)
        repo.update_conversation_stage(conv_id, "closed_lost")
        return

    # 8. Hold — no outbound, no stage change
    if decision.action == "hold":
        log.info("m7.worker.hold", conversation_id=conv_id)
        return

    # 9. Send (reply or advance_stage)
    if not decision.is_send():
        log.warning("m7.worker.send_no_message", conversation_id=conv_id, action=decision.action)
        return

    # ─────────────────────────────────────────────────────────────
    # Operator-mode replies (2026-06-24 default).
    # Rather than typing the reply into IG via Playwright, queue it
    # to Discord. The operator pastes + sends from their phone, then
    # confirms via /reply_sent <pending_id>. Only on that confirmation
    # do we persist the outbound message + advance the stage. This
    # keeps the AI's authorship inside an audit trail (the pending
    # row) without faking an IG send that didn't happen.
    # ─────────────────────────────────────────────────────────────
    operator_mode = os.getenv("M7_OPERATOR_MODE", "true").lower() == "true"

    if operator_mode:
        from app.core.supabase_client import get_supabase
        sb = get_supabase()
        sb.table("pending_outbound_messages").insert({
            "conversation_id": conv_id,
            "prospect_handle": prospect["handle"],
            "ig_account": ig_account,
            "message_text": decision.next_message or "",
            "stage_at_decision": stage,
            "agent_decision_json": decision.model_dump(),
            "ai_confidence": decision.confidence,
            "history_snapshot": history,
            "status": "awaiting_operator",
        }).execute()
        log.info(
            "m7.worker.queued_for_operator",
            conversation_id=conv_id,
            confidence=decision.confidence,
            chars=len(decision.next_message or ""),
        )
        # Persistence + stage advance happen when operator confirms via
        # /reply_sent (see m8_handoff/discord_bot.py::reply_sent handler).
        return

    # ── Legacy Playwright reply path (only when M7_OPERATOR_MODE=false) ──
    allowed, sent, cap = can_send_dm_today(ig_account)
    if not allowed:
        log.warning("m7.worker.dm_cap_reached",
                    ig_account=ig_account, sent=sent, cap=cap)
        return

    try:
        ig_msg_id = await send_dm(ig_account, prospect["handle"], decision.next_message or "")
    except Exception as e:
        log.error("m7.worker.send_failed", conversation_id=conv_id, err=str(e))
        repo.create_handoff(
            conversation_id=conv_id,
            prospect_id=prospect["id"],
            trigger_reason="low_confidence",
            trigger_detail=f"DM send failed: {e}",
            conversation_snapshot=history,
            ai_recommended_action=decision.next_message,
        )
        repo.update_conversation_stage(conv_id, "handed_off", human_intervention=True)
        return

    # 10. Persist outbound + stage transition (legacy path)
    repo.insert_outbound_message(
        conversation_id=conv_id,
        body=decision.next_message or "",
        stage_at_time=stage,
        agent_decision=decision.model_dump(),
        ai_confidence=decision.confidence,
        triggered_handoff=False,
        ig_message_id=ig_msg_id,
    )
    repo.touch_conversation_outbound(conv_id)

    try:
        new_stage = next_stage(stage, decision)
    except IllegalTransition as e:
        log.error("m7.worker.illegal_transition", conversation_id=conv_id, err=str(e))
        new_stage = stage   # stay put on illegal transition; will retry next turn

    # Rolling confidence average
    rolling = _rolling_confidence_avg(
        prior=conversation.get("ai_confidence_avg"),
        new_sample=decision.confidence * 100,
        n_samples=outbound_count + 1,
    )

    new_micro = _maybe_append(
        conversation.get("micro_commitments_obtained") or [],
        decision.micro_commitment_obtained,
    )
    new_objections = _with_objection(objections_handled, decision.detected_objection)

    repo.update_conversation_stage(
        conv_id,
        new_stage=new_stage,
        micro_commitments=new_micro,
        objections_handled=new_objections,
        ai_confidence_rolling=rolling,
    )

    log.info(
        "m7.worker.turn_done",
        conversation_id=conv_id,
        old_stage=stage,
        new_stage=new_stage,
        confidence=decision.confidence,
        outbound=outbound_count + 1,
    )


# ────────────────────────────────────────────────────────────────────
# Ghost follow-up scheduling
# ────────────────────────────────────────────────────────────────────

async def schedule_ghost_followups() -> None:
    """
    For every active conversation that has been silent past the cadence,
    schedule the next ghost follow-up from Mason's 5 templates.
    """
    convs = repo.list_active_conversations()
    now = datetime.now(timezone.utc)
    scheduled_count = 0

    for conv in convs:
        ghost_count = conv.get("ghost_followup_count") or 0
        if ghost_count >= len(GHOST_FOLLOWUP_TEMPLATES):
            continue  # exhausted the 5 templates → leave alone

        # When did we last hear from them or talk to them?
        last_inbound = _parse_ts(conv.get("last_inbound_at"))
        last_outbound = _parse_ts(conv.get("last_outbound_at"))
        anchor = max([t for t in (last_inbound, last_outbound) if t], default=None)
        if not anchor:
            continue

        wait_hours = GHOST_FOLLOWUP_CADENCE_HOURS[ghost_count]
        due_at = anchor + timedelta(hours=wait_hours)
        if due_at > now:
            continue  # not yet due

        # Avoid double-scheduling: check if there's already a scheduled followup
        # for this conversation at this number.
        # (skipping the DB check here for brevity; the followups table has
        #  no uniqueness on (conv_id, number) but the followup_worker can
        #  dedupe at send time)

        repo.schedule_followup(
            conversation_id=conv["id"],
            followup_number=ghost_count + 1,
            scheduled_for=now,  # send immediately on next followup_worker tick
            message_template=GHOST_FOLLOWUP_TEMPLATES[ghost_count],
        )
        scheduled_count += 1

    if scheduled_count:
        log.info("m7.worker.followups_scheduled", count=scheduled_count)


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────

def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _rolling_confidence_avg(prior: Optional[float], new_sample: float, n_samples: int) -> float:
    if not prior or n_samples <= 1:
        return new_sample
    return ((prior * (n_samples - 1)) + new_sample) / n_samples


def _maybe_append(arr: list[str], item: Optional[str]) -> list[str]:
    if item is None or item in arr:
        return arr
    return arr + [item]


def _with_objection(arr: list[str], obj: Optional[str]) -> list[str]:
    if not obj or obj == "other":
        return arr
    return arr + [obj]


# ────────────────────────────────────────────────────────────────────
# Main loop
# ────────────────────────────────────────────────────────────────────

async def cycle_for_account(ig_account: str) -> None:
    """One poll → process → schedule pass for one IG account."""
    log.info("m7.worker.cycle.start", ig_account=ig_account)

    try:
        unread_threads = await poll_inbox(ig_account)
    except Exception as e:
        log.error("m7.worker.poll_failed", ig_account=ig_account, err=str(e))
        return

    for thread in unread_threads:
        prospect_handle = thread["prospect_handle"]
        # Look up the conversation by prospect_handle → qualified_prospect → conversation
        # The conversations table doesn't store prospect_handle directly, so:
        #   1. Find qualified_prospect by handle
        #   2. Find conversation by prospect_id
        # If no qualified_prospect exists, the inbound is from someone we didn't open
        # with — skip (could be cold inbound; M8 surfaces those separately).
        from app.core.supabase_client import get_supabase  # type: ignore
        sb = get_supabase()
        qp = (sb.table("qualified_prospects").select("*")
              .eq("handle", prospect_handle).limit(1).execute()).data
        if not qp:
            log.info("m7.worker.unknown_prospect_skipped",
                     ig_account=ig_account, prospect=prospect_handle)
            continue
        prospect = qp[0]

        conv = repo.get_conversation_by_prospect(prospect["id"])
        if not conv:
            # First reply to our opener — create the conversation row
            conv = repo.create_conversation(
                prospect_id=prospect["id"],
                ig_account=ig_account,
                starting_stage="opener",
            )
            log.info(
                "m7.worker.conversation_created",
                conversation_id=conv["id"],
                prospect=prospect_handle,
            )

        # Process each new inbound message (oldest first)
        for msg in thread["messages"]:
            # Dedupe: skip if we already have this exact body as an inbound
            # for this conversation in the last 24h.
            if _already_recorded(conv["id"], msg["body"]):
                continue
            try:
                await _handle_inbound(conv, prospect, msg["body"], ig_account)
            except Exception as e:
                log.error("m7.worker.handle_inbound_failed",
                          conversation_id=conv["id"], err=str(e), exc_info=True)
            # Re-load the conversation row in case stage advanced
            conv = repo.get_conversation(conv["id"]) or conv

    # After processing all inbounds, schedule ghost followups
    await schedule_ghost_followups()

    log.info("m7.worker.cycle.done", ig_account=ig_account)


def _already_recorded(conversation_id: str, body: str) -> bool:
    """Cheap dedupe — same body as an existing inbound in this conversation."""
    from app.core.supabase_client import get_supabase  # type: ignore
    sb = get_supabase()
    r = (sb.table("messages").select("id")
         .eq("conversation_id", conversation_id)
         .eq("direction", "inbound")
         .eq("body", body)
         .limit(1).execute())
    return bool(r.data)


async def main_loop() -> None:
    log.info("m7.worker.start", poll_interval=POLL_INTERVAL_SECONDS)
    from app.core.supabase_client import get_supabase  # type: ignore

    while True:
        sb = get_supabase()
        accts = (sb.table("ig_accounts").select("handle")
                 .eq("current_status", "active").execute()).data or []
        if not accts:
            log.warning("m7.worker.no_active_ig_accounts — sleeping")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            continue

        for a in accts:
            try:
                await cycle_for_account(a["handle"])
            except Exception as e:
                log.error("m7.worker.cycle_unhandled",
                          ig_account=a["handle"], err=str(e), exc_info=True)
            await asyncio.sleep(BETWEEN_ACCOUNTS_SECONDS)

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main_loop())
