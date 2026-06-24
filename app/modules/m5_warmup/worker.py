"""
M5 Warm-up Worker.

Main loop:
1. Plan: for any qualified prospect with an opener that doesn't yet have a warm-up
   plan, create the warming_actions rows.
2. Execute: pick warming_actions where status='scheduled' and scheduled_for <= now,
   run them via Playwright.
3. Sleep, repeat.

Run by docker-compose service `worker_warm`.
"""
from __future__ import annotations
import asyncio
import os
from app.core.logging import configure_logging, get_logger
from .planner import plan_batch
from .executor import execute_batch

configure_logging()
log = get_logger("m5.worker")

OPERATOR_ACCOUNT = os.getenv("M5_OPERATOR_ACCOUNT", "ignorethisdump2")
PLAN_INTERVAL_SECONDS = 5 * 60     # plan new prospects every 5 min
EXECUTE_INTERVAL_SECONDS = 60      # check for due actions every 1 min


async def planner_loop():
    log.info("m5.planner_loop.start", interval=PLAN_INTERVAL_SECONDS,
             operator=OPERATOR_ACCOUNT)
    while True:
        try:
            inserted = plan_batch(limit=5, ig_account=OPERATOR_ACCOUNT)
            if inserted:
                log.info("m5.planner_loop.planned", actions_added=inserted)
            else:
                log.info("m5.planner_loop.idle")
        except Exception as e:
            log.error("m5.planner_loop.error", err=str(e), exc_info=True)
        await asyncio.sleep(PLAN_INTERVAL_SECONDS)


async def executor_loop():
    log.info("m5.executor_loop.start", interval=EXECUTE_INTERVAL_SECONDS,
             operator=OPERATOR_ACCOUNT)
    while True:
        try:
            executed = await execute_batch(ig_account=OPERATOR_ACCOUNT)
            if executed:
                log.info("m5.executor_loop.cycle", actions_executed=executed)
        except Exception as e:
            log.error("m5.executor_loop.error", err=str(e), exc_info=True)
        await asyncio.sleep(EXECUTE_INTERVAL_SECONDS)


async def worker_loop():
    log.info("m5.worker.start", operator=OPERATOR_ACCOUNT)
    await asyncio.gather(planner_loop(), executor_loop())


if __name__ == "__main__":
    asyncio.run(worker_loop())
