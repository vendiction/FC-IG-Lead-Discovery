"""M2 hashtag worker. Processes due hashtags one at a time."""
from __future__ import annotations
import asyncio
from app.core.logging import configure_logging, get_logger
from .scraper import get_due_hashtags, scrape_one

configure_logging()
log = get_logger("m2.worker")

IDLE_POLL_SECONDS = 600   # 10 min between checks when no due hashtags
BETWEEN_TAGS_SECONDS = 120  # 2 min between hashtag scrapes


async def worker_loop():
    log.info("m2.worker.start")
    while True:
        due = get_due_hashtags()
        if not due:
            log.info("m2.worker.idle")
            await asyncio.sleep(IDLE_POLL_SECONDS)
            continue

        for tag_row in due:
            try:
                await scrape_one(tag_row)
            except Exception as e:
                log.error("m2.worker.unhandled",
                          tag=tag_row.get("tag"), err=str(e), exc_info=True)
            await asyncio.sleep(BETWEEN_TAGS_SECONDS)


if __name__ == "__main__":
    asyncio.run(worker_loop())
