"""Entry point: python -m adapters.wa_notifier"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

from adapters.wa_notifier.config import load
from adapters.wa_notifier.notifier import WhatsAppNotifier


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("wa_notifier")

    try:
        cfg = load()
    except ValueError as exc:
        log.error("Config error: %s", exc)
        sys.exit(1)

    if not cfg.db_path.exists():
        log.error("Database not found: %s", cfg.db_path)
        sys.exit(1)

    notifier = WhatsAppNotifier(cfg)

    loop = asyncio.new_event_loop()

    def _shutdown(sig: int, _frame: object) -> None:
        log.info("Received signal %s, shutting down...", signal.Signals(sig).name)
        notifier.stop()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        loop.run_until_complete(notifier.run())
    finally:
        loop.close()
        log.info("WhatsApp notifier stopped.")


if __name__ == "__main__":
    main()
