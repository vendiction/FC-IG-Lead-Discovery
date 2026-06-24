"""
Cooldown reaper.

When M5's executor catches an IG soft-block, it flips the IG account to
`current_status='cooldown'` with `cooldown_until = now + 2h`. Nothing in
the original codebase ever flipped it back to `'active'` once that
timestamp passed — accounts would silently stay in cooldown forever
unless a human ran SQL.

This module is the missing reaper. It's called periodically by the
scheduler (every 5 min by default).

Design notes:
- `decide_reapable_accounts()` is a pure function over rows + a "now"
  timestamp. Easy to unit test without a DB.
- `reap_expired_cooldowns()` is the DB-touching wrapper. It loads the
  set of cooldown rows, runs `decide_reapable_accounts()`, and issues
  one UPDATE per reapable row.

Schema columns touched on ig_accounts:
- current_status: 'cooldown' → 'active'
- cooldown_until: cleared (NULL)
- last_soft_block_at: untouched (audit trail)
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional
import structlog

from app.core.supabase_client import get_supabase

log = structlog.get_logger("core.cooldown_reaper")


@dataclass(frozen=True)
class CooldownRow:
    """Subset of ig_accounts columns the reaper cares about."""

    handle: str
    current_status: str
    cooldown_until: Optional[datetime]


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    """Parse a Postgres/ISO timestamp into a tz-aware datetime, or None."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        log.warning("cooldown_reaper.bad_timestamp", value=ts)
        return None


def decide_reapable_accounts(
    rows: Iterable[CooldownRow],
    *,
    now: datetime,
) -> list[str]:
    """
    Pure: return the handles whose cooldowns have expired and should be
    flipped back to 'active'.

    A row is reapable iff:
    - current_status == 'cooldown'
    - cooldown_until is set
    - cooldown_until <= now

    Rows without `cooldown_until` are NOT reaped — those are indefinite
    cooldowns set manually and must be cleared by a human.
    """
    out: list[str] = []
    for row in rows:
        if row.current_status != "cooldown":
            continue
        if row.cooldown_until is None:
            continue
        if row.cooldown_until <= now:
            out.append(row.handle)
    return out


def reap_expired_cooldowns(*, now: Optional[datetime] = None) -> int:
    """
    Load all cooldown rows from ig_accounts, decide which are reapable,
    and flip them back to active. Returns the number reaped.

    Idempotent — running it twice in quick succession is fine.
    """
    sb = get_supabase()
    now = now or datetime.now(timezone.utc)

    raw = (
        sb.table("ig_accounts")
        .select("handle,current_status,cooldown_until")
        .eq("current_status", "cooldown")
        .execute()
    ).data or []

    rows = [
        CooldownRow(
            handle=r["handle"],
            current_status=r["current_status"],
            cooldown_until=_parse_ts(r.get("cooldown_until")),
        )
        for r in raw
    ]

    reapable = decide_reapable_accounts(rows, now=now)
    if not reapable:
        log.info("cooldown_reaper.nothing_to_reap", checked=len(rows))
        return 0

    # One UPDATE per row keeps the SQL trivially auditable and lets us
    # log each transition individually. Batch sizes here are tiny (a
    # handful of burner accounts max), so per-row cost is fine.
    reaped = 0
    for handle in reapable:
        sb.table("ig_accounts").update(
            {"current_status": "active", "cooldown_until": None}
        ).eq("handle", handle).eq("current_status", "cooldown").execute()
        log.info("cooldown_reaper.reaped", handle=handle)
        reaped += 1

    log.info("cooldown_reaper.cycle_done", reaped=reaped, checked=len(rows))
    return reaped
