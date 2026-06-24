"""
DEPRECATED — M0 placeholder that was never replaced when M7 shipped.

Until 2026-06-24 the docker-compose `worker_conv` service launched this file,
which silently slept and made it look like M7 was running. The real worker is
in `app.modules.m7_conversation.worker`. docker-compose.yml has been updated
to point at the real one.

This file is left in place so any old container or script that still references
the path fails loudly instead of silently sleeping.
"""
from __future__ import annotations
import sys
from app.core.logging import configure_logging, get_logger

configure_logging()
log = get_logger("workers.conversation")


def main():
    log.error(
        "workers.conversation.deprecated_entrypoint_invoked",
        message=(
            "This is the deprecated M0 placeholder. Run "
            "`python -m app.modules.m7_conversation.worker` instead. "
            "Exiting non-zero so the container restart loop surfaces this."
        ),
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
