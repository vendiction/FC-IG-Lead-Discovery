"""
Qualifier worker: runs M3 (cross-platform research) and M4 (scoring).

Run by docker-compose service `worker_qual`.
"""
from __future__ import annotations
import asyncio
from app.modules.m3_cross_platform.worker import worker_loop as m3_loop
from app.modules.m4_qualifier.worker import worker_loop as m4_loop
from app.core.logging import configure_logging, get_logger

configure_logging()
log = get_logger("workers.qualifier")


async def main():
    log.info("workers.qualifier.start")
    await asyncio.gather(m3_loop(), m4_loop())


if __name__ == "__main__":
    asyncio.run(main())
