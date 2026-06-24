"""
Discovery worker: M1 tagged crawler + M2 hashtag scraper running concurrently.

Run by docker-compose service `worker_disc`.
"""
from __future__ import annotations
import asyncio
from app.modules.m1_tagged_crawler.worker import worker_loop as m1_loop
from app.modules.m2_hashtag_scraper.worker import worker_loop as m2_loop
from app.core.logging import configure_logging, get_logger

configure_logging()
log = get_logger("workers.discovery")


async def main():
    log.info("workers.discovery.start")
    await asyncio.gather(
        m1_loop(),
        m2_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
