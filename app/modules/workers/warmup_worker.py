"""Warmup worker placeholder — replaced when M5 ships."""
import asyncio
from app.core.logging import configure_logging, get_logger
configure_logging()
log = get_logger("workers.warmup")
async def main():
    log.info("workers.warmup.placeholder — M5 not yet built, sleeping")
    while True:
        await asyncio.sleep(3600)
if __name__ == "__main__":
    asyncio.run(main())
