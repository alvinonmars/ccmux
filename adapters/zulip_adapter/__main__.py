"""Zulip adapter entry point.

Usage:
    python -m adapters.zulip_adapter
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

from .adapter import ZulipAdapter
from .config import load

log = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        cfg = load()
    except ValueError as e:
        log.error("Configuration error: %s", e)
        return 1

    log.info(
        "Zulip adapter starting: site=%s streams=%d",
        cfg.site,
        len(cfg.streams),
    )

    adapter = ZulipAdapter(cfg)

    # Clean stale PID files from previous runs
    cleaned = adapter.process_mgr.clean_stale_pids()
    if cleaned:
        log.info("Cleaned %d stale PID file(s)", cleaned)

    loop = asyncio.new_event_loop()

    def _shutdown(sig: int, frame) -> None:
        log.info("Received signal %d, shutting down...", sig)
        adapter.stop()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        loop.run_until_complete(adapter.run())
    except KeyboardInterrupt:
        log.info("Interrupted")
    finally:
        adapter.stop()
        loop.close()

    log.info("Zulip adapter stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
