"""
M1 worker entry point.

Continuously claims handles from crawl_queue and processes them.
Run as the `worker_disc` service in docker-compose.

Also provides CLI commands to seed the queue from the `seeds` table.
"""
from __future__ import annotations
import argparse
import asyncio
import os
import socket
import sys
from app.core.logging import configure_logging, get_logger
from app.core.supabase_client import get_supabase
from .queue import claim_next, enqueue, queue_depth
from .crawler import process_one

configure_logging()
log = get_logger("m1.worker")

POLL_IDLE_SECONDS = 30
WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"


async def worker_loop():
    log.info("m1.worker.start", worker_id=WORKER_ID)
    idle_logged = False

    while True:
        row = claim_next(WORKER_ID)
        if not row:
            if not idle_logged:
                depth = queue_depth()
                log.info("m1.worker.idle", queue=depth)
                idle_logged = True
            await asyncio.sleep(POLL_IDLE_SECONDS)
            continue

        idle_logged = False
        try:
            await process_one(row)
        except Exception as e:
            log.error("m1.worker.unhandled", err=str(e), handle=row["handle"], exc_info=True)
            from .queue import mark_failed
            mark_failed(row["id"], f"unhandled: {e}", retry=True)


def cmd_seed_from_table() -> None:
    """Enqueue all active seeds with depth=0."""
    sb = get_supabase()
    seeds = (sb.table("seeds").select("handle").eq("active", True).execute()).data
    added = 0
    for s in seeds:
        if enqueue(s["handle"], depth=0, parent_seed=None, priority=1):
            added += 1
    log.info("m1.seeds.enqueued", total=len(seeds), newly_added=added)


def cmd_status() -> None:
    print(queue_depth())


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("run", help="Run worker loop (default)")
    sub.add_parser("seed", help="Enqueue all active seeds")
    sub.add_parser("status", help="Show queue depth")

    p_enq = sub.add_parser("enqueue", help="Enqueue a single handle")
    p_enq.add_argument("handle")
    p_enq.add_argument("--depth", type=int, default=0)
    p_enq.add_argument("--priority", type=int, default=1)

    args = parser.parse_args()

    if args.cmd == "seed":
        cmd_seed_from_table()
    elif args.cmd == "status":
        cmd_status()
    elif args.cmd == "enqueue":
        ok = enqueue(args.handle, depth=args.depth, priority=args.priority)
        print(f"{'added' if ok else 'already in queue'}: @{args.handle}")
    else:
        asyncio.run(worker_loop())


if __name__ == "__main__":
    main()
