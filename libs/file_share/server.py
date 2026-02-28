"""Async HTTP file share server (aiohttp).

Run as: python -m libs.file_share.server
"""
from __future__ import annotations

import html
import json
import logging
import sys
import time
import tomllib
from collections import defaultdict, OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image as _PILImage
from PIL.ExifTags import Base as _ExifBase

from aiohttp import web

from libs.file_share.share_manager import ShareManager
from libs.file_share.thumbnails import IMAGE_EXTENSIONS

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

# ---------------------------------------------------------------------------
# Rate limiter (simple in-memory per-IP)
# ---------------------------------------------------------------------------

_rate_buckets: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT = 500  # requests per minute


def _check_rate(ip: str) -> bool:
    now = time.monotonic()
    bucket = _rate_buckets[ip]
    # Prune old entries
    _rate_buckets[ip] = bucket = [t for t in bucket if now - t < 60]
    if len(bucket) >= RATE_LIMIT:
        return False
    bucket.append(now)
    return True


# ---------------------------------------------------------------------------
# Access logger
# ---------------------------------------------------------------------------

_access_log_path: Path | None = None


def _init_access_log(log_dir: Path) -> None:
    global _access_log_path
    log_dir.mkdir(parents=True, exist_ok=True)
    _access_log_path = log_dir / "access.jsonl"


def _log_access(
    request: web.Request, share_id: str, action: str, filename: str = "",
) -> None:
    if _access_log_path is None:
        return
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ip": request.headers.get("CF-Connecting-IP", request.remote or "unknown"),
        "country": request.headers.get("CF-IPCountry", ""),
        "share_id": share_id,
        "action": action,
        "filename": filename,
        "user_agent": request.headers.get("User-Agent", ""),
    }
    with open(_access_log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------

_template_cache: dict[str, str] = {}


def _load_template(name: str) -> str:
    if name not in _template_cache:
        _template_cache[name] = (TEMPLATE_DIR / name).read_text()
    return _template_cache[name]


def _render(title: str, body: str, meta: str = "", extra_head: str = "") -> str:
    base = _load_template("base.html")
    return base.format(title=title, body=body, meta=meta, extra_head=extra_head)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Cache EXIF dates per share to avoid re-reading on every request
_exif_date_cache: dict[str, dict[str, str]] = {}


def _get_photo_date(filepath: Path) -> str:
    """Extract date from EXIF (DateTimeOriginal) or fall back to mtime.

    Returns date string like '2026-02-15'.
    """
    try:
        with _PILImage.open(filepath) as img:
            exif = img.getexif()
            # DateTimeOriginal (tag 36867) or DateTimeDigitized (36868)
            for tag_id in (36867, 36868, _ExifBase.DateTime):
                val = exif.get(tag_id)
                if val:
                    # EXIF format: "2026:02:15 14:30:00"
                    return val[:10].replace(":", "-")
    except Exception:
        pass
    # Fallback to file mtime
    mtime = datetime.fromtimestamp(filepath.stat().st_mtime, tz=timezone.utc)
    return mtime.strftime("%Y-%m-%d")


def _get_files_grouped_by_date(
    share_id: str, share_path: Path,
) -> OrderedDict[str, list[Path]]:
    """Return files grouped by date, newest first. Uses cache."""
    files = sorted(
        (f for f in share_path.iterdir()
         if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS),
        key=lambda f: f.name,
    )

    # Build or reuse date cache for this share
    if share_id not in _exif_date_cache:
        _exif_date_cache[share_id] = {}
    cache = _exif_date_cache[share_id]

    groups: dict[str, list[Path]] = defaultdict(list)
    for f in files:
        if f.name not in cache:
            cache[f.name] = _get_photo_date(f)
        groups[cache[f.name]].append(f)

    # Sort by date descending (newest first)
    return OrderedDict(sorted(groups.items(), reverse=True))


def _format_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TB"


def _safe_resolve(base: Path, filename: str) -> Path | None:
    """Resolve filename under base dir, preventing path traversal."""
    resolved = (base / filename).resolve()
    if not str(resolved).startswith(str(base.resolve())):
        return None
    return resolved


def _is_image_share(share_path: Path) -> bool:
    """Check if the share directory is predominantly images."""
    if not share_path.is_dir():
        return False
    files = list(share_path.iterdir())
    if not files:
        return False
    image_count = sum(
        1 for f in files if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS
    )
    return image_count > len(files) * 0.5


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _get_share_or_403(
    manager: ShareManager, share_id: str, request: web.Request,
) -> web.Response | dict:
    """Validate token from query param. Return share dict or 403 Response."""
    token = request.query.get("token", "")
    if not token:
        return web.Response(status=403, text="Missing token")
    share = manager.validate_token(share_id, token)
    if share is None:
        return web.Response(status=403, text="Invalid or expired share")
    return share


def _check_password(
    share: dict, request: web.Request,
) -> web.Response | None:
    """If share requires password and no valid session cookie, return login page."""
    if not share.get("password"):
        return None
    cookie_name = f"fs_auth_{share['id']}"
    cookie = request.cookies.get(cookie_name)
    if cookie == share["password"]:
        return None
    # Show login page
    tpl = _load_template("login.html")
    body = tpl.format(
        share_id=share["id"],
        token=share["token"],
        error_msg="",
    )
    return web.Response(
        status=200,
        content_type="text/html",
        text=_render(title="Login Required", body=body),
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def create_app(manager: ShareManager) -> web.Application:
    """Build the aiohttp application with all routes."""

    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def share_landing(request: web.Request) -> web.Response:
        ip = request.remote or "unknown"
        if not _check_rate(ip):
            return web.Response(status=429, text="Rate limit exceeded")

        share_id = request.match_info["share_id"]
        result = _get_share_or_403(manager, share_id, request)
        if isinstance(result, web.Response):
            return result
        share = result

        pw_page = _check_password(share, request)
        if pw_page is not None:
            return pw_page

        share_path = Path(share["path"])
        token = share["token"]

        _log_access(request, share_id, "browse")

        if share["mode"] == "readonly" and _is_image_share(share_path):
            return _render_gallery(share, share_path, token)
        return _render_filelist(share, share_path, token)

    async def password_login(request: web.Request) -> web.Response:
        share_id = request.match_info["share_id"]
        token = request.query.get("token", "")
        share = manager.validate_token(share_id, token)
        if share is None:
            return web.Response(status=403, text="Invalid share")

        data = await request.post()
        password = data.get("password", "")

        if password != share.get("password"):
            tpl = _load_template("login.html")
            body = tpl.format(
                share_id=share["id"],
                token=token,
                error_msg='<div class="msg error">Incorrect password</div>',
            )
            return web.Response(
                content_type="text/html",
                text=_render(title="Login Required", body=body),
            )

        # Set auth cookie and redirect to share
        resp = web.HTTPFound(f"/s/{share_id}?token={token}")
        resp.set_cookie(
            f"fs_auth_{share_id}",
            password,
            max_age=86400,
            httponly=True,
            samesite="Lax",
        )
        raise resp

    async def serve_thumb(request: web.Request) -> web.Response:
        # No rate limiting on thumbnails — lightweight static files, high concurrency expected
        share_id = request.match_info["share_id"]
        filename = request.match_info["filename"]
        result = _get_share_or_403(manager, share_id, request)
        if isinstance(result, web.Response):
            return result
        share = result

        thumb_dir = share.get("thumbnail_dir")
        if not thumb_dir:
            return web.Response(status=404, text="No thumbnails")

        path = _safe_resolve(Path(thumb_dir), filename)
        if path is None or not path.is_file():
            return web.Response(status=404, text="Not found")

        return web.FileResponse(path)

    async def serve_file(request: web.Request) -> web.Response:
        ip = request.remote or "unknown"
        if not _check_rate(ip):
            return web.Response(status=429, text="Rate limit exceeded")

        share_id = request.match_info["share_id"]
        filename = request.match_info["filename"]
        result = _get_share_or_403(manager, share_id, request)
        if isinstance(result, web.Response):
            return result
        share = result

        pw_page = _check_password(share, request)
        if pw_page is not None:
            return pw_page

        share_path = Path(share["path"])
        path = _safe_resolve(share_path, filename)
        if path is None or not path.is_file():
            return web.Response(status=404, text="Not found")

        _log_access(request, share_id, "download", filename)
        return web.FileResponse(path)

    async def upload_file(request: web.Request) -> web.Response:
        ip = request.remote or "unknown"
        if not _check_rate(ip):
            return web.Response(status=429, text="Rate limit exceeded")

        share_id = request.match_info["share_id"]
        result = _get_share_or_403(manager, share_id, request)
        if isinstance(result, web.Response):
            return result
        share = result

        if share["mode"] != "writable":
            return web.Response(status=403, text="Upload not allowed")

        pw_page = _check_password(share, request)
        if pw_page is not None:
            return pw_page

        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "file":
            return web.Response(status=400, text="No file provided")

        filename = field.filename or "upload"
        # Sanitize filename
        filename = Path(filename).name
        if not filename:
            return web.Response(status=400, text="Invalid filename")

        # Check extension whitelist
        allowed_ext = share.get("allowed_extensions")
        if allowed_ext:
            ext = Path(filename).suffix.lstrip(".").lower()
            if ext not in allowed_ext:
                return web.Response(
                    status=400,
                    text=f"File type .{ext} not allowed. Allowed: {', '.join(allowed_ext)}",
                )

        # Check size limit
        max_size = share.get("max_upload_size_mb")
        max_bytes = max_size * 1024 * 1024 if max_size else None

        share_path = Path(share["path"])
        dest = share_path / filename
        # Avoid overwriting — append counter
        counter = 1
        while dest.exists():
            stem = Path(filename).stem
            suffix = Path(filename).suffix
            dest = share_path / f"{stem}_{counter}{suffix}"
            counter += 1

        size = 0
        with open(dest, "wb") as f:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                size += len(chunk)
                if max_bytes and size > max_bytes:
                    f.close()
                    dest.unlink(missing_ok=True)
                    return web.Response(
                        status=400,
                        text=f"File exceeds size limit ({max_size}MB)",
                    )
                f.write(chunk)

        _log_access(request, share_id, "upload", dest.name)
        token = share["token"]
        raise web.HTTPFound(f"/s/{share_id}?token={token}")

    app = web.Application(client_max_size=200 * 1024 * 1024)  # 200MB max
    app.router.add_get("/health", health)
    app.router.add_get("/s/{share_id}", share_landing)
    app.router.add_post("/s/{share_id}/login", password_login)
    app.router.add_get("/s/{share_id}/thumbs/{filename}", serve_thumb)
    app.router.add_get("/s/{share_id}/files/{filename}", serve_file)
    app.router.add_post("/s/{share_id}/upload", upload_file)
    return app


# ---------------------------------------------------------------------------
# Page renderers
# ---------------------------------------------------------------------------

def _render_gallery(share: dict, share_path: Path, token: str) -> web.Response:
    """Render image gallery page with date grouping and infinite scroll."""
    tpl = _load_template("gallery.html")
    thumb_dir = share.get("thumbnail_dir")
    share_id = share["id"]

    date_groups = _get_files_grouped_by_date(share_id, share_path)

    # Build JSON data for all photos — JS handles progressive rendering
    groups_js = []
    total_count = 0
    for date_str, files in date_groups.items():
        total_count += len(files)
        photos = []
        for f in files:
            name = f.name
            file_url = f"/s/{share_id}/files/{name}?token={token}"
            if thumb_dir and (Path(thumb_dir) / f"{f.stem}.jpg").exists():
                thumb_url = f"/s/{share_id}/thumbs/{f.stem}.jpg?token={token}"
            else:
                thumb_url = file_url
            photos.append({"name": name, "url": file_url, "thumb": thumb_url})
        groups_js.append({"date": date_str, "count": len(files), "photos": photos})

    body = tpl.format(
        file_count=total_count,
        date_count=len(date_groups),
        groups_json=json.dumps(groups_js),
    )
    return web.Response(
        content_type="text/html",
        text=_render(
            title=html.escape(share.get("label") or share_id),
            body=body,
            meta=(
                f'<span class="badge">{total_count} photos</span>'
                f'<span class="badge">{len(date_groups)} days</span>'
            ),
        ),
    )


def _render_filelist(share: dict, share_path: Path, token: str) -> web.Response:
    """Render file list / upload page."""
    tpl = _load_template("filelist.html")
    share_id = share["id"]

    files = sorted(f for f in share_path.iterdir() if f.is_file())

    rows = []
    for f in files:
        name = html.escape(f.name)
        stat = f.stat()
        size = _format_size(stat.st_size)
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M"
        )
        dl_url = f"/s/{share_id}/files/{f.name}?token={token}"
        rows.append(
            f"<tr><td>{name}</td><td class='size'>{size}</td>"
            f"<td class='date'>{mtime}</td>"
            f'<td><a href="{dl_url}" download class="btn btn-primary" '
            f'style="padding:4px 10px;font-size:0.8em">Download</a></td></tr>'
        )

    upload_form = ""
    upload_script = ""
    if share["mode"] == "writable":
        upload_url = f"/s/{share_id}/upload?token={token}"
        upload_form = (
            f'<div class="upload-zone" id="upload-zone">'
            f'<form method="POST" action="{upload_url}" enctype="multipart/form-data" id="upload-form">'
            f'<p>Drag files here or <label for="file-input" style="color:#2563eb;cursor:pointer">'
            f'browse</label></p>'
            f'<input type="file" name="file" id="file-input">'
            f'<button type="submit" class="btn btn-primary" style="margin-top:10px">Upload</button>'
            f'</form></div>'
        )
        upload_script = """
<script>
const zone = document.getElementById('upload-zone');
const input = document.getElementById('file-input');
zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('dragover'); });
zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
zone.addEventListener('drop', e => {
  e.preventDefault(); zone.classList.remove('dragover');
  input.files = e.dataTransfer.files;
  document.getElementById('upload-form').submit();
});
input.addEventListener('change', () => { if (input.files.length) document.getElementById('upload-form').submit(); });
</script>"""

    body = tpl.format(
        file_count=len(files),
        rows="\n".join(rows),
        upload_form=upload_form,
        upload_script=upload_script,
    )
    return web.Response(
        content_type="text/html",
        text=_render(
            title=html.escape(share.get("label") or share_id),
            body=body,
            meta=f'{len(files)} files' + (' | Upload enabled' if share["mode"] == "writable" else ''),
        ),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    config_path = PROJECT_ROOT / "ccmux.toml"
    with open(config_path, "rb") as f:
        return tomllib.load(f)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = _load_config()
    fs_config = config.get("file_share", {})
    port = fs_config.get("port", 9800)
    registry = Path(fs_config.get("registry", "~/.ccmux/data/file_shares.json")).expanduser()

    log_dir = Path(fs_config.get("log_dir", "~/.ccmux/data/file_share_logs")).expanduser()
    _init_access_log(log_dir)

    manager = ShareManager(registry)
    app = create_app(manager)

    logger.info("Starting file share server on 127.0.0.1:%d", port)
    web.run_app(app, host="127.0.0.1", port=port, print=None)


if __name__ == "__main__":
    main()
