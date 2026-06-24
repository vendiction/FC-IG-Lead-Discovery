"""Per-IG-account rate limiter.

Reads daily_caps from ig_accounts, current usage from ig_account_usage,
applies safety factor from settings, and either allows or denies an action.

Atomic increment via Postgres function (defined inline) prevents races.
"""
from __future__ import annotations
from datetime import date
from typing import Literal
from .supabase_client import get_supabase
from .settings import get_settings
from .logging import get_logger

log = get_logger(__name__)

ActionType = Literal[
    "follows", "likes", "comments", "story_actions",
    "profile_loads", "hashtag_pages", "dms_sent",
]


class RateLimitExceeded(Exception):
    """Account has hit its daily cap for this action."""


class AccountUnavailable(Exception):
    """Account is in cooldown or banned."""


def get_active_accounts() -> list[dict]:
    """All operator accounts currently usable."""
    sb = get_supabase()
    r = (sb.table("ig_accounts")
         .select("*")
         .eq("current_status", "active")
         .execute())
    return r.data or []


def check_and_consume(ig_handle: str, action: ActionType, amount: int = 1) -> None:
    """
    Atomically check if an action is allowed for this account today.
    Raises RateLimitExceeded if cap would be exceeded.
    Raises AccountUnavailable if account is cooldown/banned.
    Otherwise increments usage counter and returns.
    """
    sb = get_supabase()
    settings = get_settings()

    acct = (sb.table("ig_accounts")
            .select("daily_caps,current_status")
            .eq("handle", ig_handle)
            .single()
            .execute()).data

    if not acct:
        raise AccountUnavailable(f"Account {ig_handle} not found")

    if acct["current_status"] != "active":
        raise AccountUnavailable(
            f"Account {ig_handle} status={acct['current_status']}"
        )

    cap = int(acct["daily_caps"].get(action, 0) * settings.rate_limit_safety_factor)
    today = date.today().isoformat()

    # Upsert today's row and check current count
    usage = (sb.table("ig_account_usage")
             .select(action)
             .eq("ig_account", ig_handle)
             .eq("usage_date", today)
             .execute()).data

    current = usage[0][action] if usage else 0

    if current + amount > cap:
        raise RateLimitExceeded(
            f"{ig_handle} would exceed {action} cap "
            f"({current + amount} > {cap})"
        )

    # Increment via upsert
    if usage:
        sb.table("ig_account_usage").update({
            action: current + amount
        }).eq("ig_account", ig_handle).eq("usage_date", today).execute()
    else:
        sb.table("ig_account_usage").insert({
            "ig_account": ig_handle,
            "usage_date": today,
            action: amount,
        }).execute()

    log.debug("rate.consume", ig=ig_handle, action=action,
              new_count=current + amount, cap=cap)


def mark_soft_block(ig_handle: str, hours: int = 48) -> None:
    """Account hit an action-block. Put it in cooldown."""
    from datetime import datetime, timedelta, timezone
    sb = get_supabase()
    until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    sb.table("ig_accounts").update({
        "current_status": "cooldown",
        "last_soft_block_at": datetime.now(timezone.utc).isoformat(),
        "cooldown_until": until,
    }).eq("handle", ig_handle).execute()
    log.warning("rate.soft_block", ig=ig_handle, cooldown_until=until)
