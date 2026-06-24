"""
M7 — Repository layer for conversations, messages, and followups.

All Supabase access lives here. The worker never touches the DB directly.

Assumes `app.core.supabase_client.get_supabase()` exists and returns a
configured supabase-py v2 Client (consistent with M4's scorer setup).
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional
import structlog

from app.core.supabase_client import get_supabase  # type: ignore

log = structlog.get_logger("m7.repo")


# ────────────────────────────────────────────────────────────────────
# Conversations
# ────────────────────────────────────────────────────────────────────

def get_conversation_by_prospect(prospect_id: str) -> Optional[dict]:
    sb = get_supabase()
    r = sb.table("conversations").select("*").eq("prospect_id", prospect_id).limit(1).execute()
    return r.data[0] if r.data else None


def get_conversation(conversation_id: str) -> Optional[dict]:
    sb = get_supabase()
    r = sb.table("conversations").select("*").eq("id", conversation_id).limit(1).execute()
    return r.data[0] if r.data else None


def list_active_conversations(ig_account: Optional[str] = None) -> list[dict]:
    """All conversations not in a terminal stage."""
    sb = get_supabase()
    q = sb.table("conversations").select("*").not_.in_(
        "current_stage", ["closed_won", "closed_lost", "ghosted", "handed_off"]
    )
    if ig_account:
        q = q.eq("ig_account", ig_account)
    return q.execute().data or []


def create_conversation(
    prospect_id: str,
    ig_account: str,
    starting_stage: str = "opener",
) -> dict:
    sb = get_supabase()
    r = sb.table("conversations").insert({
        "prospect_id": prospect_id,
        "ig_account": ig_account,
        "current_stage": starting_stage,
    }).execute()
    return r.data[0]


def update_conversation_stage(
    conversation_id: str,
    new_stage: str,
    micro_commitments: Optional[list[str]] = None,
    objections_handled: Optional[list[str]] = None,
    ai_confidence_rolling: Optional[float] = None,
    human_intervention: Optional[bool] = None,
) -> None:
    sb = get_supabase()
    patch: dict = {
        "current_stage": new_stage,
        "stage_entered_at": datetime.now(timezone.utc).isoformat(),
    }
    if micro_commitments is not None:
        patch["micro_commitments_obtained"] = micro_commitments
    if objections_handled is not None:
        patch["objections_handled"] = objections_handled
    if ai_confidence_rolling is not None:
        patch["ai_confidence_avg"] = round(ai_confidence_rolling, 2)
    if human_intervention is not None:
        patch["human_intervention"] = human_intervention
    sb.table("conversations").update(patch).eq("id", conversation_id).execute()


def touch_conversation_inbound(conversation_id: str) -> None:
    """Update last_inbound_at to now."""
    sb = get_supabase()
    sb.table("conversations").update({
        "last_inbound_at": datetime.now(timezone.utc).isoformat(),
        "ghost_followup_count": 0,  # any inbound resets the ghost counter
    }).eq("id", conversation_id).execute()


def touch_conversation_outbound(conversation_id: str) -> None:
    sb = get_supabase()
    sb.table("conversations").update({
        "last_outbound_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", conversation_id).execute()


def increment_ghost_count(conversation_id: str) -> int:
    """Returns the new count."""
    sb = get_supabase()
    current = (sb.table("conversations").select("ghost_followup_count")
               .eq("id", conversation_id).single().execute()).data
    new_count = (current["ghost_followup_count"] or 0) + 1
    sb.table("conversations").update(
        {"ghost_followup_count": new_count}
    ).eq("id", conversation_id).execute()
    return new_count


# ────────────────────────────────────────────────────────────────────
# Messages
# ────────────────────────────────────────────────────────────────────

def list_messages(conversation_id: str) -> list[dict]:
    sb = get_supabase()
    r = (sb.table("messages").select("*")
         .eq("conversation_id", conversation_id)
         .order("created_at", desc=False)
         .execute())
    return r.data or []


def insert_inbound_message(
    conversation_id: str,
    body: str,
    stage_at_time: str,
    ig_message_id: Optional[str] = None,
    received_at: Optional[datetime] = None,
) -> dict:
    sb = get_supabase()
    r = sb.table("messages").insert({
        "conversation_id": conversation_id,
        "direction": "inbound",
        "body": body,
        "received_at": (received_at or datetime.now(timezone.utc)).isoformat(),
        "stage_at_time": stage_at_time,
        "ig_message_id": ig_message_id,
    }).execute()
    return r.data[0]


def insert_outbound_message(
    conversation_id: str,
    body: str,
    stage_at_time: str,
    agent_decision: dict,
    ai_confidence: float,
    triggered_handoff: bool = False,
    sent_at: Optional[datetime] = None,
    ig_message_id: Optional[str] = None,
) -> dict:
    sb = get_supabase()
    r = sb.table("messages").insert({
        "conversation_id": conversation_id,
        "direction": "outbound",
        "body": body,
        "sent_at": (sent_at or datetime.now(timezone.utc)).isoformat(),
        "stage_at_time": stage_at_time,
        "agent_decision": agent_decision,
        "ai_confidence": round(ai_confidence * 100, 2),  # store as 0-100 to match column
        "triggered_handoff": triggered_handoff,
        "ig_message_id": ig_message_id,
    }).execute()
    return r.data[0]


def count_outbound_messages(conversation_id: str) -> int:
    sb = get_supabase()
    r = (sb.table("messages").select("id", count="exact")
         .eq("conversation_id", conversation_id)
         .eq("direction", "outbound")
         .execute())
    return r.count or 0


# ────────────────────────────────────────────────────────────────────
# Followups
# ────────────────────────────────────────────────────────────────────

def schedule_followup(
    conversation_id: str,
    followup_number: int,
    scheduled_for: datetime,
    message_template: str,
) -> dict:
    sb = get_supabase()
    r = sb.table("followups").insert({
        "conversation_id": conversation_id,
        "followup_number": followup_number,
        "scheduled_for": scheduled_for.isoformat(),
        "message_template": message_template,
    }).execute()
    return r.data[0]


def get_due_followups(now: Optional[datetime] = None) -> list[dict]:
    sb = get_supabase()
    cutoff = (now or datetime.now(timezone.utc)).isoformat()
    r = (sb.table("followups").select("*")
         .eq("status", "scheduled")
         .lte("scheduled_for", cutoff)
         .execute())
    return r.data or []


def mark_followup_sent(followup_id: str) -> None:
    sb = get_supabase()
    sb.table("followups").update({
        "status": "sent",
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", followup_id).execute()


def cancel_followups_for_conversation(
    conversation_id: str,
    reason: str = "cancelled_response",
) -> int:
    """Called when prospect replies — cancel all pending followups. Returns count."""
    sb = get_supabase()
    r = (sb.table("followups").update({"status": reason})
         .eq("conversation_id", conversation_id)
         .eq("status", "scheduled")
         .execute())
    return len(r.data) if r.data else 0


# ────────────────────────────────────────────────────────────────────
# Handoffs (writes only — Discord bot reads from M8 module)
# ────────────────────────────────────────────────────────────────────

def create_handoff(
    conversation_id: str,
    prospect_id: str,
    trigger_reason: str,
    trigger_detail: str,
    conversation_snapshot: list[dict],
    ai_recommended_action: Optional[str] = None,
) -> dict:
    sb = get_supabase()
    r = sb.table("handoffs").insert({
        "conversation_id": conversation_id,
        "prospect_id": prospect_id,
        "trigger_reason": trigger_reason,
        "trigger_detail": trigger_detail,
        "conversation_snapshot": conversation_snapshot,
        "ai_recommended_action": ai_recommended_action,
    }).execute()
    return r.data[0]


# ────────────────────────────────────────────────────────────────────
# Qualified prospects (read-only for M7)
# ────────────────────────────────────────────────────────────────────

def get_qualified_prospect(prospect_id: str) -> Optional[dict]:
    sb = get_supabase()
    r = (sb.table("qualified_prospects").select("*")
         .eq("id", prospect_id).limit(1).execute())
    return r.data[0] if r.data else None


def list_prospects_with_sent_opener_awaiting_reply(ig_account: str) -> list[dict]:
    """Prospects whose opener was sent but who don't have a conversation row yet."""
    sb = get_supabase()
    r = (sb.table("qualified_prospects").select("*")
         .eq("status", "opener_ready")  # M6 sets this when opener is sent
         .execute())
    return r.data or []
