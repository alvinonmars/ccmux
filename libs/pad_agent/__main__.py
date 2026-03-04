"""CLI entrypoint: python -m libs.pad_agent"""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="KidPad monitor")
    parser.add_argument("--child", help="Child name (overrides config)")
    args = parser.parse_args()

    from .config import load
    from .monitor import PadMonitor

    cfg = load()
    if args.child:
        from dataclasses import replace

        cfg = replace(cfg, child_name=args.child)

    monitor = PadMonitor(cfg)
    monitor.run()


if __name__ == "__main__":
    main()
