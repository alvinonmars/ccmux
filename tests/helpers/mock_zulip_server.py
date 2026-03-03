"""Mock Zulip server for integration tests.

Stdlib-only HTTP server implementing the Zulip API subset needed by the
Zulip adapter. Runs on a random port, no third-party dependencies.

Usage:
    server = MockZulipServer()
    server.start()
    # ... test code using server.base_url ...
    server.stop()

Or as a context manager:
    with MockZulipServer() as server:
        ...
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


class _ZulipState:
    """Shared mutable state for the mock server."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._event_queue: list[dict] = []
        self._posted_messages: list[dict] = []
        self._updated_messages: list[dict] = []
        self._uploaded_files: list[dict] = []
        self._topic_history: dict[str, list[dict]] = {}  # "stream/topic" -> messages
        self._upload_files: dict[str, bytes] = {}  # "/user_uploads/..." -> content
        self._queue_counter = 0
        self._event_id_counter = 0

    def inject_event(self, event: dict) -> None:
        """Queue a Zulip event for delivery via GET /events."""
        with self.lock:
            self._event_id_counter += 1
            event.setdefault("id", self._event_id_counter)
            self._event_queue.append(event)

    def inject_message_event(
        self,
        stream: str,
        topic: str,
        content: str,
        sender_email: str = "user@example.com",
        sender_full_name: str = "Test User",
    ) -> None:
        """Convenience: inject a stream message event."""
        self.inject_event({
            "type": "message",
            "message": {
                "type": "stream",
                "sender_email": sender_email,
                "sender_full_name": sender_full_name,
                "display_recipient": stream,
                "subject": topic,
                "content": content,
                "timestamp": int(time.time()),
            },
        })

    def drain_events(self) -> list[dict]:
        """Return and clear all queued events."""
        with self.lock:
            events = list(self._event_queue)
            self._event_queue.clear()
            return events

    def get_posted_messages(self) -> list[dict]:
        """Return all messages posted via POST /messages."""
        with self.lock:
            return list(self._posted_messages)

    def get_updated_messages(self) -> list[dict]:
        """Return all messages updated via PATCH /messages/:id."""
        with self.lock:
            return list(self._updated_messages)

    def get_uploaded_files(self) -> list[dict]:
        """Return metadata of all uploaded files."""
        with self.lock:
            return list(self._uploaded_files)

    def set_topic_history(self, stream: str, topic: str, messages: list[dict]) -> None:
        """Set canned response for GET /messages with stream+topic narrow."""
        with self.lock:
            self._topic_history[f"{stream}/{topic}"] = messages

    def add_upload_file(self, path: str, content: bytes) -> None:
        """Register a file to serve from /user_uploads/."""
        with self.lock:
            self._upload_files[path] = content

    def clear(self) -> None:
        """Reset all state."""
        with self.lock:
            self._event_queue.clear()
            self._posted_messages.clear()
            self._updated_messages.clear()
            self._uploaded_files.clear()
            self._topic_history.clear()
            self._upload_files.clear()
            self._queue_counter = 0
            self._event_id_counter = 0

    def next_queue_id(self) -> str:
        with self.lock:
            self._queue_counter += 1
            return f"queue-{self._queue_counter}"


def _make_handler(state: _ZulipState) -> type:
    """Create a request handler class bound to shared state."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # Suppress request logging in tests

        def _send_json(self, data: dict, status: int = 200) -> None:
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length", 0))
            return self.rfile.read(length) if length else b""

        def _parse_form(self) -> dict[str, str]:
            body = self._read_body().decode()
            return dict(urllib.parse.parse_qsl(body))

        def do_POST(self) -> None:
            if self.path == "/api/v1/register":
                self._handle_register()
            elif self.path == "/api/v1/messages":
                self._handle_post_message()
            elif self.path == "/api/v1/user_uploads":
                self._handle_upload()
            else:
                self._send_json({"result": "error", "msg": "not found"}, 404)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/v1/events":
                self._handle_get_events(parsed.query)
            elif parsed.path == "/api/v1/messages":
                self._handle_get_messages(parsed.query)
            elif parsed.path.startswith("/user_uploads/"):
                self._handle_get_upload(parsed.path)
            else:
                self._send_json({"result": "error", "msg": "not found"}, 404)

        def do_PATCH(self) -> None:
            # PATCH /api/v1/messages/:id
            import re
            m = re.match(r"/api/v1/messages/(\d+)", self.path)
            if m:
                self._handle_update_message(int(m.group(1)))
            else:
                self._send_json({"result": "error", "msg": "not found"}, 404)

        def do_DELETE(self) -> None:
            if self.path == "/api/v1/events":
                # Delete event queue — just acknowledge
                self._send_json({"result": "success"})
            else:
                self._send_json({"result": "error", "msg": "not found"}, 404)

        def _handle_register(self) -> None:
            queue_id = state.next_queue_id()
            self._send_json({
                "result": "success",
                "queue_id": queue_id,
                "last_event_id": -1,
            })

        def _handle_get_events(self, query: str) -> None:
            params = dict(urllib.parse.parse_qsl(query))
            # Short poll: return events immediately or empty after brief wait
            events = state.drain_events()
            if not events:
                # Brief wait to simulate long-poll (but short for tests)
                time.sleep(0.1)
                events = state.drain_events()
            self._send_json({
                "result": "success",
                "events": events,
            })

        def _handle_post_message(self) -> None:
            form = self._parse_form()
            with state.lock:
                msg_id = len(state._posted_messages) + 1
                record = {
                    "id": msg_id,
                    "type": form.get("type", ""),
                    "to": form.get("to", ""),
                    "topic": form.get("topic", ""),
                    "content": form.get("content", ""),
                    "timestamp": time.time(),
                }
                state._posted_messages.append(record)
            self._send_json({"result": "success", "id": msg_id})

        def _handle_get_messages(self, query: str) -> None:
            params = dict(urllib.parse.parse_qsl(query))
            narrow_str = params.get("narrow", "[]")
            try:
                narrow = json.loads(narrow_str)
            except json.JSONDecodeError:
                narrow = []

            # Extract stream and topic from narrow
            stream = ""
            topic = ""
            for item in narrow:
                if isinstance(item, list) and len(item) == 2:
                    if item[0] == "stream":
                        stream = item[1]
                    elif item[0] == "topic":
                        topic = item[1]

            key = f"{stream}/{topic}"
            with state.lock:
                messages = state._topic_history.get(key, [])

            self._send_json({
                "result": "success",
                "messages": messages,
            })

        def _handle_get_upload(self, path: str) -> None:
            with state.lock:
                content = state._upload_files.get(path)
            if content is not None:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_response(404)
                self.end_headers()

        def _handle_update_message(self, message_id: int) -> None:
            form = self._parse_form()
            with state.lock:
                state._updated_messages.append({
                    "id": message_id,
                    "content": form.get("content", ""),
                    "timestamp": time.time(),
                })
            self._send_json({"result": "success"})

        def _handle_upload(self) -> None:
            body = self._read_body()
            # Extract filename from multipart (simplified parsing)
            filename = "uploaded_file"
            content_type = self.headers.get("Content-Type", "")
            if "boundary=" in content_type:
                boundary = content_type.split("boundary=")[1].strip()
                text = body.decode("latin-1")
                if 'filename="' in text:
                    start = text.index('filename="') + len('filename="')
                    end = text.index('"', start)
                    filename = text[start:end]

            uri = f"/user_uploads/1/test/{filename}"
            with state.lock:
                state._uploaded_files.append({
                    "filename": filename,
                    "uri": uri,
                    "size": len(body),
                })
                state._upload_files[uri] = body
            self._send_json({"result": "success", "uri": uri})

    return Handler


class MockZulipServer:
    """Mock Zulip HTTP server for integration tests."""

    def __init__(self) -> None:
        self.state = _ZulipState()
        self._handler_class = _make_handler(self.state)
        self._server = HTTPServer(("127.0.0.1", 0), self._handler_class)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        """Start the server in a background thread."""
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="mock-zulip-server",
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the server."""
        self._server.shutdown()
        if self._thread:
            self._thread.join(timeout=5)

    # Convenience delegates to state
    def inject_event(self, event: dict) -> None:
        self.state.inject_event(event)

    def inject_message_event(self, *args, **kwargs) -> None:
        self.state.inject_message_event(*args, **kwargs)

    def get_posted_messages(self) -> list[dict]:
        return self.state.get_posted_messages()

    def get_updated_messages(self) -> list[dict]:
        return self.state.get_updated_messages()

    def get_uploaded_files(self) -> list[dict]:
        return self.state.get_uploaded_files()

    def set_topic_history(self, stream: str, topic: str, messages: list[dict]) -> None:
        self.state.set_topic_history(stream, topic, messages)

    def add_upload_file(self, path: str, content: bytes) -> None:
        self.state.add_upload_file(path, content)

    def clear(self) -> None:
        self.state.clear()

    def __enter__(self) -> MockZulipServer:
        self.start()
        return self

    def __exit__(self, *args) -> None:
        self.stop()
