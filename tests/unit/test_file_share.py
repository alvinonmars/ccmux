"""Unit tests for libs.file_share module."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from libs.file_share.share_manager import ShareManager
from libs.file_share.thumbnails import generate_thumbnails


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    return tmp_path / "file_shares.json"


@pytest.fixture
def share_dir(tmp_path: Path) -> Path:
    d = tmp_path / "shared_folder"
    d.mkdir()
    # Create some dummy files
    (d / "photo1.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
    (d / "photo2.png").write_bytes(b"\x89PNG" + b"\x00" * 100)
    (d / "readme.txt").write_text("hello")
    return d


@pytest.fixture
def manager(registry: Path) -> ShareManager:
    return ShareManager(registry)


# ---------------------------------------------------------------------------
# ShareManager CRUD
# ---------------------------------------------------------------------------


class TestShareManagerCreate:
    def test_create_readonly_share(self, manager: ShareManager, share_dir: Path) -> None:
        share = manager.create_share("test-share", str(share_dir), mode="readonly", label="Test")
        assert share["id"] == "test-share"
        assert share["mode"] == "readonly"
        assert share["label"] == "Test"
        assert len(share["token"]) > 20
        assert share["password"] is None

    def test_create_writable_share(self, manager: ShareManager, tmp_path: Path) -> None:
        upload_dir = tmp_path / "uploads" / "new-folder"
        share = manager.create_share("upload-test", str(upload_dir), mode="writable")
        assert share["mode"] == "writable"
        assert upload_dir.is_dir()  # Should be auto-created

    def test_create_with_password(self, manager: ShareManager, share_dir: Path) -> None:
        share = manager.create_share("pw-share", str(share_dir), password="secret123")
        assert share["password"] == "secret123"

    def test_create_with_extensions(self, manager: ShareManager, share_dir: Path) -> None:
        share = manager.create_share(
            "ext-share", str(share_dir),
            allowed_extensions=["pdf", "docx"],
            max_upload_size_mb=50,
        )
        assert share["allowed_extensions"] == ["pdf", "docx"]
        assert share["max_upload_size_mb"] == 50

    def test_create_duplicate_id_raises(self, manager: ShareManager, share_dir: Path) -> None:
        manager.create_share("dup", str(share_dir))
        with pytest.raises(ValueError, match="already exists"):
            manager.create_share("dup", str(share_dir))

    def test_create_invalid_mode_raises(self, manager: ShareManager, share_dir: Path) -> None:
        with pytest.raises(ValueError, match="Invalid mode"):
            manager.create_share("bad", str(share_dir), mode="execute")

    def test_create_nonexistent_readonly_path_raises(self, manager: ShareManager, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="does not exist"):
            manager.create_share("missing", str(tmp_path / "nodir"), mode="readonly")


class TestShareManagerGet:
    def test_get_existing_share(self, manager: ShareManager, share_dir: Path) -> None:
        created = manager.create_share("s1", str(share_dir))
        fetched = manager.get_share("s1")
        assert fetched is not None
        assert fetched["id"] == created["id"]
        assert fetched["token"] == created["token"]

    def test_get_nonexistent_returns_none(self, manager: ShareManager) -> None:
        assert manager.get_share("no-such-share") is None

    def test_get_expired_share_returns_none(self, manager: ShareManager, share_dir: Path) -> None:
        manager.create_share("old", str(share_dir), expires_days=0)
        # The share was created with expires_days=0 so it expires instantly
        # (actually within the same second, so we patch time)
        data = json.loads(manager._path.read_text())
        data["shares"][0]["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()
        manager._path.write_text(json.dumps(data))
        assert manager.get_share("old") is None


class TestShareManagerList:
    def test_list_returns_active_only(self, manager: ShareManager, share_dir: Path) -> None:
        manager.create_share("active1", str(share_dir))
        manager.create_share("active2", str(share_dir).rstrip("/"))
        # Manually expire one
        data = json.loads(manager._path.read_text())
        data["shares"][1]["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()
        manager._path.write_text(json.dumps(data))
        shares = manager.list_shares()
        assert len(shares) == 1
        assert shares[0]["id"] == "active1"

    def test_list_empty_registry(self, manager: ShareManager) -> None:
        assert manager.list_shares() == []


class TestShareManagerDelete:
    def test_delete_existing(self, manager: ShareManager, share_dir: Path) -> None:
        manager.create_share("to-delete", str(share_dir))
        assert manager.delete_share("to-delete") is True
        assert manager.get_share("to-delete") is None

    def test_delete_nonexistent(self, manager: ShareManager) -> None:
        assert manager.delete_share("no-such") is False


class TestShareManagerValidateToken:
    def test_valid_token(self, manager: ShareManager, share_dir: Path) -> None:
        created = manager.create_share("v1", str(share_dir))
        result = manager.validate_token("v1", created["token"])
        assert result is not None
        assert result["id"] == "v1"

    def test_wrong_token(self, manager: ShareManager, share_dir: Path) -> None:
        manager.create_share("v2", str(share_dir))
        assert manager.validate_token("v2", "wrong-token") is None

    def test_expired_token(self, manager: ShareManager, share_dir: Path) -> None:
        manager.create_share("v3", str(share_dir))
        data = json.loads(manager._path.read_text())
        data["shares"][0]["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()
        manager._path.write_text(json.dumps(data))
        assert manager.validate_token("v3", data["shares"][0]["token"]) is None

    def test_nonexistent_share(self, manager: ShareManager) -> None:
        assert manager.validate_token("nope", "any-token") is None


class TestShareManagerCleanup:
    def test_cleanup_removes_expired(self, manager: ShareManager, share_dir: Path) -> None:
        manager.create_share("keep", str(share_dir))
        manager.create_share("expire1", str(share_dir).rstrip("/"))
        # Expire the second one
        data = json.loads(manager._path.read_text())
        data["shares"][1]["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()
        manager._path.write_text(json.dumps(data))

        removed = manager.cleanup_expired()
        assert removed == 1
        assert len(manager.list_shares()) == 1

    def test_cleanup_nothing_to_remove(self, manager: ShareManager, share_dir: Path) -> None:
        manager.create_share("still-good", str(share_dir))
        assert manager.cleanup_expired() == 0


# ---------------------------------------------------------------------------
# ShareManager persistence
# ---------------------------------------------------------------------------


class TestShareManagerPersistence:
    def test_registry_created_on_init(self, registry: Path) -> None:
        ShareManager(registry)
        assert registry.exists()
        data = json.loads(registry.read_text())
        assert data == {"shares": []}

    def test_data_persists_across_instances(self, registry: Path, share_dir: Path) -> None:
        m1 = ShareManager(registry)
        created = m1.create_share("persist", str(share_dir))
        m2 = ShareManager(registry)
        fetched = m2.get_share("persist")
        assert fetched is not None
        assert fetched["token"] == created["token"]


# ---------------------------------------------------------------------------
# Path traversal prevention (server helper)
# ---------------------------------------------------------------------------


class TestPathTraversal:
    def test_safe_resolve_normal(self, share_dir: Path) -> None:
        from libs.file_share.server import _safe_resolve
        result = _safe_resolve(share_dir, "photo1.jpg")
        assert result is not None
        assert result == share_dir / "photo1.jpg"

    def test_safe_resolve_traversal_blocked(self, share_dir: Path) -> None:
        from libs.file_share.server import _safe_resolve
        result = _safe_resolve(share_dir, "../../../etc/passwd")
        assert result is None

    def test_safe_resolve_dot_dot_in_name(self, share_dir: Path) -> None:
        from libs.file_share.server import _safe_resolve
        result = _safe_resolve(share_dir, "..%2f..%2fetc%2fpasswd")
        # The encoded dots shouldn't resolve outside; pathlib handles this
        assert result is None or str(result).startswith(str(share_dir.resolve()))


# ---------------------------------------------------------------------------
# Thumbnail generation
# ---------------------------------------------------------------------------


class TestThumbnailGeneration:
    def test_generate_thumbnails_creates_files(self, tmp_path: Path) -> None:
        src = tmp_path / "images"
        src.mkdir()
        thumb = tmp_path / "thumbs"

        # Create a minimal valid JPEG (1x1 pixel)
        from PIL import Image
        img = Image.new("RGB", (100, 80), color="red")
        img.save(src / "test.jpg", "JPEG")
        img.save(src / "test2.png", "PNG")

        count = generate_thumbnails(src, thumb)
        assert count == 2
        assert (thumb / "test.jpg").exists()
        assert (thumb / "test2.jpg").exists()

    def test_generate_thumbnails_idempotent(self, tmp_path: Path) -> None:
        src = tmp_path / "images"
        src.mkdir()
        thumb = tmp_path / "thumbs"

        from PIL import Image
        img = Image.new("RGB", (100, 80), color="blue")
        img.save(src / "test.jpg", "JPEG")

        count1 = generate_thumbnails(src, thumb)
        count2 = generate_thumbnails(src, thumb)
        assert count1 == 1
        assert count2 == 0  # Already exists

    def test_generate_thumbnails_skips_non_images(self, tmp_path: Path) -> None:
        src = tmp_path / "mixed"
        src.mkdir()
        thumb = tmp_path / "thumbs"

        (src / "readme.txt").write_text("not an image")
        (src / "data.csv").write_text("a,b,c")

        count = generate_thumbnails(src, thumb)
        assert count == 0

    def test_generate_thumbnails_handles_rgba(self, tmp_path: Path) -> None:
        src = tmp_path / "images"
        src.mkdir()
        thumb = tmp_path / "thumbs"

        from PIL import Image
        img = Image.new("RGBA", (100, 80), color=(255, 0, 0, 128))
        img.save(src / "transparent.png", "PNG")

        count = generate_thumbnails(src, thumb)
        assert count == 1
        # Verify saved as JPEG (no alpha)
        result = Image.open(thumb / "transparent.jpg")
        assert result.mode == "RGB"


# ---------------------------------------------------------------------------
# aiohttp server integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
def test_app(tmp_path: Path, share_dir: Path):
    """Create a test aiohttp app with a pre-configured share."""
    registry = tmp_path / "registry.json"
    mgr = ShareManager(registry)
    share = mgr.create_share("test-gallery", str(share_dir), mode="readonly", label="Test Gallery")

    from libs.file_share.server import create_app
    app = create_app(mgr)
    return app, share


@pytest.fixture
def writable_app(tmp_path: Path):
    """Create a test app with a writable share."""
    registry = tmp_path / "registry.json"
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    mgr = ShareManager(registry)
    share = mgr.create_share(
        "test-upload", str(upload_dir), mode="writable",
        allowed_extensions=["txt", "pdf"], max_upload_size_mb=1,
    )

    from libs.file_share.server import create_app
    app = create_app(mgr)
    return app, share


class TestServerRoutes:
    @pytest.mark.asyncio
    async def test_health_endpoint(self, test_app) -> None:
        from aiohttp.test_utils import TestClient, TestServer
        app, share = test_app
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/health")
            assert resp.status == 200
            text = await resp.text()
            assert text == "ok"

    @pytest.mark.asyncio
    async def test_share_landing_valid_token(self, test_app) -> None:
        from aiohttp.test_utils import TestClient, TestServer
        app, share = test_app
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(f"/s/test-gallery?token={share['token']}")
            assert resp.status == 200
            text = await resp.text()
            assert "Test Gallery" in text

    @pytest.mark.asyncio
    async def test_share_landing_missing_token(self, test_app) -> None:
        from aiohttp.test_utils import TestClient, TestServer
        app, share = test_app
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/s/test-gallery")
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_share_landing_wrong_token(self, test_app) -> None:
        from aiohttp.test_utils import TestClient, TestServer
        app, share = test_app
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/s/test-gallery?token=wrong")
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_file_download(self, test_app, share_dir: Path) -> None:
        from aiohttp.test_utils import TestClient, TestServer
        app, share = test_app
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(f"/s/test-gallery/files/readme.txt?token={share['token']}")
            assert resp.status == 200
            text = await resp.text()
            assert text == "hello"

    @pytest.mark.asyncio
    async def test_file_download_traversal_blocked(self, test_app) -> None:
        from aiohttp.test_utils import TestClient, TestServer
        app, share = test_app
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                f"/s/test-gallery/files/../../etc/passwd?token={share['token']}"
            )
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_nonexistent_share(self, test_app) -> None:
        from aiohttp.test_utils import TestClient, TestServer
        app, share = test_app
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/s/nonexistent?token=any")
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_upload_to_writable_share(self, writable_app, tmp_path: Path) -> None:
        from aiohttp import FormData
        from aiohttp.test_utils import TestClient, TestServer
        app, share = writable_app
        async with TestClient(TestServer(app)) as client:
            data = FormData()
            data.add_field("file", b"hello world", filename="test.txt", content_type="text/plain")
            resp = await client.post(
                f"/s/test-upload/upload?token={share['token']}",
                data=data,
                allow_redirects=False,
            )
            assert resp.status == 302  # Redirect after upload
            upload_dir = tmp_path / "uploads"
            assert (upload_dir / "test.txt").exists()

    @pytest.mark.asyncio
    async def test_upload_blocked_extension(self, writable_app) -> None:
        from aiohttp import FormData
        from aiohttp.test_utils import TestClient, TestServer
        app, share = writable_app
        async with TestClient(TestServer(app)) as client:
            data = FormData()
            data.add_field("file", b"exe content", filename="virus.exe", content_type="application/octet-stream")
            resp = await client.post(
                f"/s/test-upload/upload?token={share['token']}",
                data=data,
            )
            assert resp.status == 400
            text = await resp.text()
            assert "not allowed" in text

    @pytest.mark.asyncio
    async def test_upload_to_readonly_share_blocked(self, test_app) -> None:
        from aiohttp import FormData
        from aiohttp.test_utils import TestClient, TestServer
        app, share = test_app
        async with TestClient(TestServer(app)) as client:
            data = FormData()
            data.add_field("file", b"data", filename="test.txt", content_type="text/plain")
            resp = await client.post(
                f"/s/test-gallery/upload?token={share['token']}",
                data=data,
            )
            assert resp.status == 403


class TestPasswordProtection:
    @pytest.fixture
    def pw_app(self, tmp_path: Path, share_dir: Path):
        registry = tmp_path / "registry.json"
        mgr = ShareManager(registry)
        share = mgr.create_share(
            "pw-share", str(share_dir), mode="readonly",
            password="mypass", label="Protected Share",
        )
        from libs.file_share.server import create_app
        app = create_app(mgr)
        return app, share

    @pytest.mark.asyncio
    async def test_password_required(self, pw_app) -> None:
        from aiohttp.test_utils import TestClient, TestServer
        app, share = pw_app
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(f"/s/pw-share?token={share['token']}")
            assert resp.status == 200
            text = await resp.text()
            assert "Protected Share" in text

    @pytest.mark.asyncio
    async def test_password_login_success(self, pw_app) -> None:
        from aiohttp.test_utils import TestClient, TestServer
        app, share = pw_app
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                f"/s/pw-share/login?token={share['token']}",
                data={"password": "mypass"},
                allow_redirects=False,
            )
            assert resp.status == 302  # Redirect after login
            # Follow redirect with cookies
            resp2 = await client.get(
                f"/s/pw-share?token={share['token']}",
            )
            assert resp2.status == 200
            text = await resp2.text()
            assert "Protected Share" in text

    @pytest.mark.asyncio
    async def test_password_login_wrong(self, pw_app) -> None:
        from aiohttp.test_utils import TestClient, TestServer
        app, share = pw_app
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                f"/s/pw-share/login?token={share['token']}",
                data={"password": "wrong"},
            )
            assert resp.status == 200
            text = await resp.text()
            assert "Incorrect password" in text
