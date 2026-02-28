"""Entry point: python -m adapters.wework_notifier"""
from __future__ import annotations

import logging
import signal
import sys

from adapters.wework_notifier.config import load
from adapters.wework_notifier.server import WeWorkCallbackServer


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("wework_notifier")

    try:
        cfg = load()
    except (ValueError, FileNotFoundError) as exc:
        log.error("Config error: %s", exc)
        sys.exit(1)

    server = WeWorkCallbackServer(cfg)

    def _shutdown(sig: int, _frame: object) -> None:
        log.info("Received signal %s, shutting down...", signal.Signals(sig).name)
        server.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        server.run()
    except Exception:
        log.exception("WeWork notifier crashed")
        sys.exit(1)
    finally:
        log.info("WeWork notifier stopped.")


if __name__ == "__main__":
    main()
