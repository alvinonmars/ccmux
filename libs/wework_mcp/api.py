"""WeChat Work REST API client with automatic token caching.

Uses stdlib urllib only (no requests/httpx dependency).
Docs: https://developer.work.weixin.qq.com/document/path/90665
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
import urllib.error
from pathlib import Path

log = logging.getLogger(__name__)

BASE_URL = "https://qyapi.weixin.qq.com/cgi-bin"
TOKEN_TTL_BUFFER = 200  # refresh 200s before expiry


class WeWorkAPIError(Exception):
    """Raised when WeChat Work API returns a non-zero errcode."""

    def __init__(self, errcode: int, errmsg: str):
        self.errcode = errcode
        self.errmsg = errmsg
        super().__init__(f"WeWork API error {errcode}: {errmsg}")


class WeWorkAPI:
    """WeChat Work API client with in-memory token caching."""

    def __init__(self, corp_id: str, secret: str, agent_id: str):
        self._corp_id = corp_id
        self._secret = secret
        self._agent_id = agent_id
        self._token: str = ""
        self._token_expires: float = 0.0

    def _get_token(self) -> str:
        """Get access token, refreshing if expired or close to expiry."""
        now = time.time()
        if self._token and now < self._token_expires:
            return self._token

        url = (
            f"{BASE_URL}/gettoken"
            f"?corpid={self._corp_id}&corpsecret={self._secret}"
        )
        data = self._http_get(url)
        self._token = data["access_token"]
        expires_in = data.get("expires_in", 7200)
        self._token_expires = now + expires_in - TOKEN_TTL_BUFFER
        log.info("WeWork token refreshed, expires in %ds", expires_in)
        return self._token

    def send_text(self, user_id: str, content: str) -> dict:
        """Send a text message to a user.

        Args:
            user_id: WeWork user ID (e.g. "user123").
            content: Message text content.
        """
        token = self._get_token()
        url = f"{BASE_URL}/message/send?access_token={token}"
        payload = {
            "touser": user_id,
            "msgtype": "text",
            "agentid": int(self._agent_id),
            "text": {"content": content},
        }
        return self._http_post(url, payload)

    def upload_media(self, media_type: str, file_path: str) -> str:
        """Upload a temporary media file and return the media_id.

        Args:
            media_type: One of "image", "voice", "video", "file".
            file_path: Local path to the file.

        Returns:
            media_id for use in send_file.
        """
        token = self._get_token()
        url = (
            f"{BASE_URL}/media/upload"
            f"?access_token={token}&type={media_type}"
        )
        path = Path(file_path)
        filename = path.name
        content_type = self._guess_content_type(filename)

        # Build multipart form data manually (avoid requests dependency)
        boundary = "----WeWorkUploadBoundary"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="media"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
        body += path.read_bytes()
        body += f"\r\n--{boundary}--\r\n".encode()

        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read().decode())
        self._check_error(data)
        return data["media_id"]

    def send_file(self, user_id: str, media_id: str) -> dict:
        """Send a file message using a previously uploaded media_id."""
        token = self._get_token()
        url = f"{BASE_URL}/message/send?access_token={token}"
        payload = {
            "touser": user_id,
            "msgtype": "file",
            "agentid": int(self._agent_id),
            "file": {"media_id": media_id},
        }
        return self._http_post(url, payload)

    def get_user_info(self, user_id: str) -> dict:
        """Get user profile information."""
        token = self._get_token()
        url = f"{BASE_URL}/user/get?access_token={token}&userid={user_id}"
        return self._http_get(url)

    # -- HTTP helpers --

    def _http_get(self, url: str) -> dict:
        resp = urllib.request.urlopen(url, timeout=15)
        data = json.loads(resp.read().decode())
        self._check_error(data)
        return data

    def _http_post(self, url: str, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read().decode())
        self._check_error(data)
        return data

    @staticmethod
    def _check_error(data: dict) -> None:
        errcode = data.get("errcode", 0)
        if errcode != 0:
            raise WeWorkAPIError(errcode, data.get("errmsg", "unknown"))

    @staticmethod
    def _guess_content_type(filename: str) -> str:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        return {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "gif": "image/gif",
            "mp3": "audio/mpeg",
            "amr": "audio/amr",
            "mp4": "video/mp4",
            "pdf": "application/pdf",
        }.get(ext, "application/octet-stream")
