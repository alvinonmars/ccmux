"""HTTP callback server for WeChat Work message receiving.

Handles:
1. Domain ownership verification: GET /WW_verify_*.txt
2. Callback URL verification: GET /callback (echostr challenge)
3. Message receiving: POST /callback (encrypted XML → FIFO + JSONL history)
"""
from __future__ import annotations

import collections
import json
import logging
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from adapters.wework_notifier.config import WeWorkConfig
from adapters.wework_notifier.crypto import decrypt_message, verify_signature
from libs.wework_mcp.history import append_message

log = logging.getLogger(__name__)

# MsgType → content extraction
MEDIA_TYPES = {"image", "voice", "video", "file", "link", "location"}

# Dedup: track last N message IDs to skip 企业微信 retries
_DEDUP_MAXLEN = 100


class WeWorkCallbackServer:
    """WeChat Work callback HTTP server with config injection."""

    def __init__(self, config: WeWorkConfig):
        self.config = config
        self._fifo_path = config.runtime_dir / "in.wework"
        self._seen_ids: collections.deque[str] = collections.deque(maxlen=_DEDUP_MAXLEN)
        self._server: HTTPServer | None = None

    def _ensure_fifo(self) -> None:
        """Create the FIFO if it doesn't exist."""
        fifo = self._fifo_path
        fifo.parent.mkdir(parents=True, exist_ok=True)
        if not fifo.exists():
            os.mkfifo(str(fifo))
            log.info("Created FIFO: %s", fifo)

    def _write_fifo(self, payload: str) -> None:
        """Write JSON payload to FIFO (non-blocking)."""
        try:
            fd = os.open(str(self._fifo_path), os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(fd, (payload + "\n").encode())
            finally:
                os.close(fd)
        except OSError as exc:
            log.warning("FIFO write failed (no reader?): %s", exc)

    def handle_message(self, decrypted_xml: str) -> None:
        """Process a decrypted WeWork callback message XML."""
        inner = ET.fromstring(decrypted_xml)

        msg_type = inner.findtext("MsgType", "")
        msg_id = inner.findtext("MsgId", "")
        from_user = inner.findtext("FromUserName", "")
        create_time = inner.findtext("CreateTime", "")

        # Dedup
        if msg_id and msg_id in self._seen_ids:
            log.debug("Duplicate MsgId %s, skipping", msg_id)
            return
        if msg_id:
            self._seen_ids.append(msg_id)

        # Extract content based on message type
        if msg_type == "text":
            content = inner.findtext("Content", "")
        elif msg_type in MEDIA_TYPES:
            content = f"[{msg_type}]"
        elif msg_type == "event":
            event = inner.findtext("Event", "")
            content = f"[event:{event}]"
        else:
            content = f"[{msg_type}]"

        # Convert Unix timestamp to ISO
        ts_iso = ""
        if create_time:
            try:
                ts_iso = datetime.fromtimestamp(
                    int(create_time), tz=timezone.utc
                ).isoformat()
            except (ValueError, OSError):
                ts_iso = create_time

        log.info(
            "WeWork message: type=%s from=%s content=%s",
            msg_type, from_user, content[:80],
        )

        # Skip events from FIFO injection (they're system events, not user messages)
        if msg_type == "event":
            return

        # 1. Write to FIFO for daemon pickup
        fifo_payload = json.dumps({
            "channel": "wework",
            "content": f"{from_user}: {content}" if from_user else content,
            "ts": int(time.time()),
        })
        self._write_fifo(fifo_payload)

        # 2. Append to JSONL history
        append_message({
            "msg_id": msg_id,
            "msg_type": msg_type,
            "from_user": from_user,
            "to_user": "",
            "content": content,
            "create_time": ts_iso,
            "direction": "inbound",
            "agent_id": self.config.agent_id,
        })

    def run(self) -> None:
        """Start the HTTP server (blocking)."""
        self._ensure_fifo()

        # Inject config into handler class via server attribute
        HTTPServer.allow_reuse_address = True
        server = HTTPServer(("127.0.0.1", self.config.port), _Handler)
        server.wework = self  # type: ignore[attr-defined]
        self._server = server

        log.info(
            "WeWork callback server listening on 127.0.0.1:%d",
            self.config.port,
        )
        server.serve_forever()

    def shutdown(self) -> None:
        if self._server:
            self._server.shutdown()


class _Handler(BaseHTTPRequestHandler):
    """HTTP request handler — delegates to WeWorkCallbackServer."""

    @property
    def _srv(self) -> WeWorkCallbackServer:
        return self.server.wework  # type: ignore[attr-defined]

    @property
    def _cfg(self) -> WeWorkConfig:
        return self._srv.config

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        # Domain ownership verification
        if path == f"/{self._cfg.verify_file}":
            self._respond(200, self._cfg.verify_content)
            log.info("Domain verification served")
            return

        # Callback URL verification (echostr challenge)
        if path == "/callback":
            qs = parse_qs(parsed.query)
            echostr = qs.get("echostr", [None])[0]
            msg_signature = qs.get("msg_signature", [None])[0]
            timestamp = qs.get("timestamp", [""])[0]
            nonce = qs.get("nonce", [""])[0]

            if echostr:
                # Verify signature
                if msg_signature and not verify_signature(
                    self._cfg.callback_token, timestamp, nonce,
                    echostr, msg_signature,
                ):
                    log.warning("Signature verification failed for echostr")
                    self._respond(403, "signature mismatch")
                    return

                try:
                    decrypted = decrypt_message(
                        self._cfg.callback_aes_key, echostr, self._cfg.corp_id,
                    )
                    self._respond(200, decrypted)
                    log.info("Echostr challenge: OK")
                except Exception as exc:
                    log.error("Failed to decrypt echostr: %s", exc)
                    self._respond(500, "decrypt failed")
                return
            self._respond(400, "missing echostr")
            return

        self._respond(404, "not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/callback":
            qs = parse_qs(parsed.query)
            msg_signature = qs.get("msg_signature", [None])[0]
            timestamp = qs.get("timestamp", [""])[0]
            nonce = qs.get("nonce", [""])[0]

            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")

            try:
                root = ET.fromstring(body)
                encrypt_node = root.find("Encrypt")
                if encrypt_node is None or not encrypt_node.text:
                    log.warning("No <Encrypt> in callback POST")
                    self._respond(200, "success")
                    return

                encrypt_text = encrypt_node.text

                # Verify signature
                if msg_signature and not verify_signature(
                    self._cfg.callback_token, timestamp, nonce,
                    encrypt_text, msg_signature,
                ):
                    log.warning("Signature verification failed for POST")
                    self._respond(403, "signature mismatch")
                    return

                decrypted_xml = decrypt_message(
                    self._cfg.callback_aes_key, encrypt_text, self._cfg.corp_id,
                )
                self._srv.handle_message(decrypted_xml)

            except Exception as exc:
                log.error("Failed to process callback POST: %s", exc)

            # Always respond 200 to avoid 企业微信 retries
            self._respond(200, "success")
            return

        self._respond(404, "not found")

    def _respond(self, code: int, body: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format: str, *args: object) -> None:
        log.debug(format, *args)
