"""
APScheduler entry point for periodic jobs.

Registered jobs (2026-06-24):
  - ghost_followups_tick       every  5 min   → m8_handoff.ghost_followups.run_once
  - cooldown_reaper_tick       every  5 min   → core.cooldown_reaper.reap_expired_cooldowns

Bug fix history (2026-06-23):
  v1 — original called AsyncIOScheduler() with no timezone arg, hitting tzlocal,
       which failed because /usr/share/zoneinfo is missing in the Playwright
       base image.
  v2 — tried pytz; not in this env (apscheduler 3.10+ dropped it).
  v3 — use stdlib zoneinfo with the `tzdata` PyPI package, which ships IANA
       zone data as a Python package, no OS-level tzdata needed. Falls back
       to UTC if zoneinfo still can't resolve the configured zone.

Change 2026-06-24 (v4):
  - Replaced the lone _placeholder job with real periodic work.
  - Each job is wrapped in a top-level try/except so one job's exception
    can't take down the loop and silently halt the others.
"""
from __future__ import annotations
import asyncio
import os
from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.logging import configure_logging, get_logger
from app.core.cooldown_reaper import reap_expired_cooldowns
from app.modules.m8_handoff.ghost_followups import run_once as ghost_followups_run_once

configure_logging()
log = get_logger("scheduler")


TIMEZONE_NAME = os.getenv("TIMEZONE", "Asia/Manila")
try:
    TIMEZONE = ZoneInfo(TIMEZONE_NAME)
except ZoneInfoNotFoundError:
    log.warning(
        "scheduler.timezone_unresolvable_falling_back_to_utc",
        got=TIMEZONE_NAME,
        hint=(
            "Install the 'tzdata' Python package (pip install tzdata) "
            "if you want Asia/Manila to work."
        ),
    )
    TIMEZONE = dt_timezone.utc


GHOST_FOLLOWUPS_INTERVAL_MINUTES = int(
    os.getenv("SCHEDULER_GHOST_FOLLOWUPS_MINUTES", "5")
)
COOLDOWN_REAPER_INTERVAL_MINUTES = int(
    os.getenv("SCHEDULER_COOLDOWN_REAPER_MINUTES", "5")
)


# ────────────────────────────────────────────────────────────────────
# Job wrappers — isolate one job's failures from the others
# ────────────────────────────────────────────────────────────────────


async def _safe_ghost_followups_tick() -> None:
    try:
        n = await ghost_followups_run_once()
        if n:
            log.info("scheduler.ghost_followups.sent", count=n)
    except Exception as e:  # pragma: no cover — defensive
        log.error("scheduler.ghost_followups.failed", err=str(e), exc_info=True)


def _safe_cooldown_reaper_tick() -> None:
    # Sync function — supabase client is sync. APScheduler will run it
    # on the default executor without blocking the event loop.
    try:
        n = reap_expired_cooldowns()
        if n:
            log.info("scheduler.cooldown_reaper.reaped", count=n)
    except Exception as e:  # pragma: no cover — defensive
        log.error("scheduler.cooldown_reaper.failed", err=str(e), exc_info=True)


# ────────────────────────────────────────────────────────────────────
# Main loop
# ────────────────────────────────────────────────────────────────────


async def main() -> None:
    sched = AsyncIOScheduler(timezone=TIMEZONE)

    sched.add_job(
        _safe_ghost_followups_tick,
        "interval",
        minutes=GHOST_FOLLOWUPS_INTERVAL_MINUTES,
        id="ghost_followups_tick",
        coalesce=True,            # don't pile up missed runs across restarts
        max_instances=1,          # only one tick at a time — they share DM cap
    )

    sched.add_job(
        _safe_cooldown_reaper_tick,
        "interval",
        minutes=COOLDOWN_REAPER_INTERVAL_MINUTES,
        id="cooldown_reaper_tick",
        coalesce=True,
        max_instances=1,
    )

    sched.start()
    log.info(
        "scheduler.started",
        timezone=str(TIMEZONE),
        ghost_followups_minutes=GHOST_FOLLOWUPS_INTERVAL_MINUTES,
        cooldown_reaper_minutes=COOLDOWN_REAPER_INTERVAL_MINUTES,
    )

    # Stay alive — the scheduler runs in the same loop.
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
