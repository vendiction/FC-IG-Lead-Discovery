"""
M8 — Ghost follow-up sender.

Scans the `followups` table for due rows (scheduled, scheduled_for <= now)
and sends them via M7's send_dm. Increments the conversation's ghost counter.

Cancellation is handled by M7's worker: any inbound from the prospect calls
cancel_followups_for_conversation() which flips status → 'cancelled_response'.
This sender only processes rows still in 'scheduled' state.

Mason's rule: 80% of sales come from multiple touches over time — leads are
rarely dropped permanently. So we run the full 5-template ladder unless the
prospect responds.
"""
from __future__ import annotations
import asyncio
import os
import structlog

from app.core.logging import configure_logging  # type: ignore
from app.core.supabase_client import get_supabase  # type: ignore
from app.modules.m7_conversation import repository as repo
from app.modules.m7_conversation.io_dm import send_dm, can_send_dm_today

configure_logging()
log = structlog.get_logger("m8.followups")


TICK_INTERVAL_SECONDS = int(os.getenv("M8_FOLLOWUP_TICK_SECONDS", "600"))   # 10 min


async def run_once() -> int:
    """Process all due followups. Returns the number successfully sent."""
    due = repo.get_due_followups()
    if not due:
        return 0

    log.info("m8.followups.due_found", count=len(due))
    sent = 0

    for f in due:
        conv = repo.get_conversation(f["conversation_id"])
        if not conv:
            log.warning("m8.followups.no_conversation", followup_id=f["id"])
            continue

        # Skip if conversation has moved to terminal
        if conv["current_stage"] in (
            "closed_won", "closed_lost", "ghosted", "handed_off",
        ):
            sb = get_supabase()
            sb.table("followups").update({"status": "cancelled_dead"}).eq(
                "id", f["id"]
            ).execute()
            continue

        # Resolve prospect handle
        prospect = repo.get_qualified_prospect(conv["prospect_id"])
        if not prospect:
            log.warning("m8.followups.no_prospect", followup_id=f["id"])
            continue

        ig_account = conv["ig_account"]

        # ─────────────────────────────────────────────────────
        # Operator mode (2026-06-24 default): queue the followup
        # card to Discord. The /followup_sent slash command does
        # the same updates this block would have done after a
        # successful send_dm.
        # ─────────────────────────────────────────────────────
        operator_mode = os.getenv("M8_OPERATOR_MODE", "true").lower() == "true"
        if operator_mode:
            sb = get_supabase()
            sb.table("followups").update({
                # Use status='ready_for_operator' so the Discord poller
                # picks it up; the existing send-success path will fire
                # from the slash-command handler.
                "status": "ready_for_operator",
            }).eq("id", f["id"]).execute()
            log.info(
                "m8.followups.queued_for_operator",
                followup_id=f["id"],
                conversation_id=conv["id"],
                prospect=prospect["handle"],
                followup_number=f["followup_number"],
            )
            sent += 1
            continue

        # ── Legacy Playwright path ─────────────────────────────
        allowed, sent_today, cap = can_send_dm_today(ig_account)
        if not allowed:
            log.warning(
                "m8.followups.dm_cap_reached",
                ig_account=ig_account, sent=sent_today, cap=cap,
            )
            continue

        try:
            await send_dm(ig_account, prospect["handle"], f["message_template"])
        except Exception as e:
            log.error(
                "m8.followups.send_failed",
                followup_id=f["id"], err=str(e),
            )
            continue

        # Log the outbound, update conversation, mark followup sent
        repo.insert_outbound_message(
            conversation_id=conv["id"],
            body=f["message_template"],
            stage_at_time=conv["current_stage"],
            agent_decision={"source": "ghost_followup", "followup_number": f["followup_number"]},
            ai_confidence=1.0,
            triggered_handoff=False,
        )
        repo.touch_conversation_outbound(conv["id"])
        new_count = repo.increment_ghost_count(conv["id"])
        repo.mark_followup_sent(f["id"])
        sent += 1

        log.info(
            "m8.followups.sent",
            conversation_id=conv["id"],
            prospect=prospect["handle"],
            followup_number=f["followup_number"],
            ghost_count=new_count,
        )

    return sent


async def main_loop() -> None:
    log.info("m8.followups.start", tick_seconds=TICK_INTERVAL_SECONDS)
    while True:
        try:
            n = await run_once()
            if n:
                log.info("m8.followups.cycle_sent", count=n)
        except Exception as e:
            log.error("m8.followups.cycle_unhandled", err=str(e), exc_info=True)
        await asyncio.sleep(TICK_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main_loop())
