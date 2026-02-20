"""Inotify-based directory watcher for FIFO registration/deregistration.

Watches the runtime directory for in.* and out.* FIFO files being created
or deleted, and fires callbacks to the daemon.
"""
from __future__ import annotations

import asyncio
import stat
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer


def _is_input_fifo_name(name: str) -> bool:
    return name == "in" or name.startswith("in.")


def _is_output_fifo_name(name: str) -> bool:
    return name.startswith("out.")


def _is_fifo(path: Path) -> bool:
    try:
        return stat.S_SchoolIFO(path.stat().st_mode)
    except OSError:
        return False


class DirectoryWatcher:
    """Watch a directory for FIFO additions and removals.

    Callbacks receive the absolute Path of the FIFO.
    on_input_add: new in.* FIFO detected
    on_input_remove: in.* FIFO deleted
    on_output_add: new out.* FIFO detected
    on_output_remove: out.* FIFO deleted
    """

    def __init__(
        self,
        path: Path,
        loop: asyncio.AbstractEventLoop,
        on_input_add: Callable[[Path], None] | None = None,
        on_input_remove: Callable[[Path], None] | None = None,
        on_output_add: Callable[[Path], None] | None = None,
        on_output_remove: Callable[[Path], None] | None = None,
    ) -> None:
        self.path = path
        self._loop = loop
        self._on_input_add = on_input_add
        self._on_input_remove = on_input_remove
        self._on_output_add = on_output_add
        self._on_output_remove = on_output_remove
        self._observer: Observer | None = None

    def start(self) -> None:
        watcher = self

        class _Handler(FileSystemEventHandler):
            def on_created(self, event: FileSystemEvent) -> None:
                if event.is_directory:
                    return
                p = Path(event.src_path)
                name = p.name
                if _is_input_fifo_name(name) and watcher._on_input_add:
                    # Confirm it's actually a FIFO (may take a moment)
                    watcher._loop.call_soon_threadsafe(
                        watcher._on_input_add, p
                    )
                elif _is_output_fifo_name(name) and watcher._on_output_add:
                    watcher._loop.call_soon_threadsafe(
                        watcher._on_output_add, p
                    )

            def on_deleted(self, event: FileSystemEvent) -> None:
                if event.is_directory:
                    return
                p = Path(event.src_path)
                name = p.name
                if _is_input_fifo_name(name) and watcher._on_input_remove:
                    watcher._loop.call_soon_threadsafe(
                        watcher._on_input_remove, p
                    )
                elif _is_output_fifo_name(name) and watcher._on_output_remove:
                    watcher._loop.call_soon_threadsafe(
                        watcher._on_output_remove, p
                    )

        self._observer = Observer()
        self._observer.schedule(_Handler(), str(self.path), recursive=False)
        self._observer.start()

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2.0)
            self._observer = None

    def scan_existing(self) -> None:
        """Fire callbacks for all FIFOs already present in the directory."""
        if not self.path.exists():
            return
        for p in self.path.iterdir():
            if p.is_fifo():
                name = p.name
                if _is_input_fifo_name(name) and self._on_input_add:
                    self._on_input_add(p)
                elif _is_output_fifo_name(name) and self._on_output_add:
                    self._on_output_add(p)
