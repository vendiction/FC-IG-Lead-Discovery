"""
M6 — Opener sender.

This is the bridge that was missing in V1: M6 generated openers but nothing
actually sent them. The approval mechanism is just the `approved_for_send`
column — set it to true via Discord, CLI, or raw SQL, and this worker picks
the opener up and sends it.

Pipeline:
  approved opener
    ↓
  pre-flight safeguards (celebrity follower cap, daily DM cap, account active)
    ↓
  io_dm.send_dm() via M7's existing Playwright layer
    ↓
  (success) → create conversation row in stage='opener'
            → insert outbound message
            → mark opener.sent_at + qualified_prospects.status='opener_ready'
            → M7's worker now monitors inbox for this prospect
    ↓
  (failure) → mark opener.send_failure_reason, leave approved=true so a
              human can retry later by clearing sent_at

Safeguards:
  OPENER_SEND_MAX_FOLLOWER_COUNT (default 500_000)
    Refuses to send to anyone above this cap regardless of approval.
    Mason's sweet spot is 1k-200k; celebrities tank deliverability.
  per-account daily DM cap
    Reuses M7.io_dm.can_send_dm_today (reads ig_accounts.daily_caps)
  duplicate-send guard
    Only picks up openers WHERE sent_at IS NULL
"""
from __future__ import annotations
import asyncio
import os
from datetime import datetime, timezone
from typing import Optional
import structlog

from app.core.logging import configure_logging  # type: ignore
from app.core.supabase_client import get_supabase  # type: ignore
from app.modules.m7_conversation.io_dm import send_dm, can_send_dm_today
from app.modules.m7_conversation import repository as conv_repo

configure_logging()
log = structlog.get_logger("m6.sender")


# ── Config ──────────────────────────────────────────────────────────

POLL_INTERVAL_SECONDS = int(os.getenv("M6_SENDER_POLL_SECONDS", "60"))
BETWEEN_SENDS_SECONDS = int(os.getenv("M6_SENDER_BETWEEN_SENDS", "180"))   # 3min
MAX_FOLLOWER_COUNT = int(os.getenv("OPENER_SEND_MAX_FOLLOWER_COUNT", "500000"))
BATCH_SIZE = int(os.getenv("M6_SENDER_BATCH_SIZE", "5"))


# ── Pre-flight result type ──────────────────────────────────────────

class PreflightFail(Exception):
    """Raised when an opener can't be sent right now (cap, celeb, etc)."""


def preflight_check(opener: dict, prospect: dict, ig_account: str) -> None:
    """
    Returns None on pass, raises PreflightFail with a reason on fail.

    Pure function — easy to unit test.

    Celebrity check (2026-06-24): `qualified_prospects` doesn't carry
    `follower_count`, so the old `prospect.get("follower_count")` path always
    saw 0 and the cap never fired. The authoritative signal is M4's
    `is_celebrity_disqualified` boolean, which IS on `qualified_prospects`
    and is set against the same FOLLOWER_HARD_DQ_CAP M6's MAX_FOLLOWER_COUNT
    points at. We still honor a literal `follower_count` if it's somehow
    in the dict (joined query, test fixture) so the env-tunable
    OPENER_SEND_MAX_FOLLOWER_COUNT remains useful for ad-hoc tightening.
    """
    if not opener.get("approved_for_send"):
        raise PreflightFail("not approved")

    if opener.get("sent_at"):
        raise PreflightFail("already sent")

    if not opener.get("opener_text"):
        raise PreflightFail("empty opener_text")

    # Primary check: did M4 flag this prospect as celebrity-tier?
    if prospect.get("is_celebrity_disqualified"):
        raise PreflightFail(
            "celebrity tier — M4 flagged is_celebrity_disqualified=true "
            f"(reason: {prospect.get('celebrity_dq_reason') or 'no reason recorded'})"
        )

    # Secondary check: explicit follower_count if present in the dict.
    # Lets tests and joined queries tighten via OPENER_SEND_MAX_FOLLOWER_COUNT.
    follower_count = prospect.get("follower_count")
    if follower_count is not None and follower_count > MAX_FOLLOWER_COUNT:
        raise PreflightFail(
            f"follower_count {follower_count} > cap {MAX_FOLLOWER_COUNT} "
            f"(celebrity tier — Mason: avoid)"
        )

    # Daily cap is checked by send_dm internally, but we can short-circuit here
    # to avoid spinning up a browser for nothing.
    allowed, sent, cap = can_send_dm_today(ig_account)
    if not allowed:
        raise PreflightFail(f"daily DM cap reached for @{ig_account}: {sent}/{cap}")


# ── Helpers ─────────────────────────────────────────────────────────

def pick_ig_account_for(prospect_id: str) -> Optional[str]:
    """
    Which of our burner accounts is responsible for sending this prospect's
    opener? Reads the warming_actions table — whichever account warmed them
    is the same account that DMs them. Avoids "stranger-danger" pattern of
    DMing from an account the prospect has never seen.

    Returns None if no warming was done (shouldn't happen — M6 only generates
    openers for warmed prospects — but defensive).
    """
    sb = get_supabase()
    r = (sb.table("warming_actions").select("ig_account")
         .eq("prospect_id", prospect_id)
         .in_("status", ["executed", "human_completed"])
         .order("executed_at", desc=True)
         .limit(1).execute())
    if r.data:
        return r.data[0]["ig_account"]
    return None


def fallback_ig_account() -> Optional[str]:
    """Used only when warming history is missing — pick the first active account."""
    sb = get_supabase()
    r = (sb.table("ig_accounts").select("handle")
         .eq("current_status", "active").limit(1).execute())
    return r.data[0]["handle"] if r.data else None


def fetch_pending_openers(limit: int) -> list[dict]:
    """Approved openers that haven't been sent yet, oldest-approved first."""
    sb = get_supabase()
    r = (sb.table("openers").select("*")
         .eq("approved_for_send", True)
         .is_("sent_at", "null")
         .order("approved_at", desc=False)
         .limit(limit).execute())
    return r.data or []


def mark_send_success(opener_id: str) -> None:
    sb = get_supabase()
    sb.table("openers").update({
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "send_failure_reason": None,
    }).eq("id", opener_id).execute()


def mark_send_failure(opener_id: str, reason: str) -> None:
    sb = get_supabase()
    sb.table("openers").update({
        "send_failure_reason": reason[:500],
    }).eq("id", opener_id).execute()


def update_prospect_status(prospect_id: str, new_status: str) -> None:
    sb = get_supabase()
    sb.table("qualified_prospects").update(
        {"status": new_status}
    ).eq("id", prospect_id).execute()


# ── Main send flow ──────────────────────────────────────────────────

async def send_one_opener(opener: dict) -> bool:
    """Send a single opener. Returns True on success."""
    prospect_id = opener["prospect_id"]
    opener_id = opener["id"]

    prospect = conv_repo.get_qualified_prospect(prospect_id)
    if not prospect:
        log.error("m6.sender.no_prospect", opener_id=opener_id)
        mark_send_failure(opener_id, "qualified_prospect row missing")
        return False

    # Refetch handle from accounts table (qualified_prospects also has it; either is fine)
    handle = prospect["handle"]

    # Pick the IG account — prefer the one that warmed them
    ig_account = pick_ig_account_for(prospect_id) or fallback_ig_account()
    if not ig_account:
        log.error("m6.sender.no_ig_account_available", opener_id=opener_id)
        mark_send_failure(opener_id, "no active IG account available")
        return False

    # Pre-flight
    try:
        preflight_check(opener, prospect, ig_account)
    except PreflightFail as e:
        log.warning(
            "m6.sender.preflight_fail",
            opener_id=opener_id, prospect=handle, reason=str(e),
        )
        mark_send_failure(opener_id, f"preflight: {e}")
        return False

    log.info(
        "m6.sender.sending",
        opener_id=opener_id,
        prospect=handle,
        ig_account=ig_account,
        opener_chars=len(opener["opener_text"]),
    )

    # Send via M7's existing DM layer
    try:
        ig_msg_id = await send_dm(ig_account, handle, opener["opener_text"])
    except Exception as e:
        log.error("m6.sender.send_failed",
                  opener_id=opener_id, prospect=handle, err=str(e))
        mark_send_failure(opener_id, f"send_dm: {e}")
        return False

    # Success — create the conversation row + first outbound message
    try:
        existing_conv = conv_repo.get_conversation_by_prospect(prospect_id)
        if existing_conv:
            # Edge case: opener was sent before (failed mid-flight), conversation exists.
            # Don't create a duplicate — just append the outbound and update sent_at.
            conv = existing_conv
            log.info("m6.sender.conversation_already_exists",
                     conversation_id=conv["id"], prospect=handle)
        else:
            conv = conv_repo.create_conversation(
                prospect_id=prospect_id,
                ig_account=ig_account,
                starting_stage="opener",
            )
            log.info("m6.sender.conversation_created",
                     conversation_id=conv["id"], prospect=handle)

        conv_repo.insert_outbound_message(
            conversation_id=conv["id"],
            body=opener["opener_text"],
            stage_at_time="opener",
            agent_decision={"source": "approved_opener", "opener_id": opener_id},
            ai_confidence=1.0,
            triggered_handoff=False,
            ig_message_id=ig_msg_id,
        )
        conv_repo.touch_conversation_outbound(conv["id"])

        mark_send_success(opener_id)
        update_prospect_status(prospect_id, "opener_ready")

        log.info(
            "m6.sender.success",
            opener_id=opener_id,
            prospect=handle,
            conversation_id=conv["id"],
        )
        return True
    except Exception as e:
        # Send succeeded but bookkeeping failed — log loud, don't mark failure
        # (the DM did land). A human will reconcile.
        log.error(
            "m6.sender.bookkeeping_failed",
            opener_id=opener_id,
            prospect=handle,
            err=str(e),
            exc_info=True,
        )
        mark_send_failure(opener_id, f"DM sent but bookkeeping failed: {e}")
        return False


# ── Main loop ───────────────────────────────────────────────────────

async def run_once() -> int:
    """One sweep. Returns count of opener sends that succeeded."""
    pending = fetch_pending_openers(BATCH_SIZE)
    if not pending:
        return 0

    log.info("m6.sender.batch", count=len(pending))
    succeeded = 0

    for opener in pending:
        ok = await send_one_opener(opener)
        if ok:
            succeeded += 1
        # Pace between sends so we don't trip IG rate limits
        await asyncio.sleep(BETWEEN_SENDS_SECONDS)

    return succeeded


async def main_loop() -> None:
    # Operator-mode is the default delivery path as of 2026-06-24.
    # In this mode, approved openers route through the Discord bot
    # (see m8_handoff/discord_bot.py::poll_dm_sends) rather than the
    # Playwright sender below. The Playwright path is kept as legacy /
    # V2 fallback for the day we have a healthy IG burner.
    operator_mode = os.getenv("M6_OPERATOR_MODE", "true").lower() == "true"

    if operator_mode:
        log.info(
            "m6.sender.operator_mode_active",
            note="DM sends route via Discord — Playwright sender idle",
        )
        # Sleep forever; the Discord bot owns the opener queue in this mode.
        while True:
            await asyncio.sleep(3600)

    # ── Legacy Playwright path (only runs when M6_OPERATOR_MODE=false) ──
    log.info(
        "m6.sender.start",
        poll_seconds=POLL_INTERVAL_SECONDS,
        between_sends=BETWEEN_SENDS_SECONDS,
        max_follower_count=MAX_FOLLOWER_COUNT,
    )
    while True:
        try:
            n = await run_once()
            if n:
                log.info("m6.sender.cycle_done", succeeded=n)
        except Exception as e:
            log.error("m6.sender.cycle_unhandled", err=str(e), exc_info=True)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main_loop())
