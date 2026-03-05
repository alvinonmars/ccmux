"""Unit tests for Zulip adapter file_handler — image preprocessing."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from adapters.zulip_adapter.file_handler import (
    IMAGE_EXTENSIONS_CONVERT,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_DIMENSION,
    VIEWABLE_EXTENSIONS,
    preprocess_image,
)


class TestPreprocessImage:
    """Tests for preprocess_image()."""

    def test_returns_none_for_non_image(self, tmp_path: Path) -> None:
        """Non-image files are ignored."""
        f = tmp_path / "readme.txt"
        f.write_text("hello")
        assert preprocess_image(f) is None

    def test_returns_none_for_small_viewable(self, tmp_path: Path) -> None:
        """Small JPEGs need no processing."""
        from PIL import Image

        f = tmp_path / "small.jpg"
        img = Image.new("RGB", (100, 100), "red")
        img.save(f, "JPEG", quality=50)
        # File should be well under 200KB
        assert f.stat().st_size < MAX_IMAGE_BYTES
        assert preprocess_image(f) is None

    def test_converts_bmp_to_jpeg(self, tmp_path: Path) -> None:
        """BMP files get converted to JPEG."""
        from PIL import Image

        f = tmp_path / "photo.bmp"
        img = Image.new("RGB", (200, 200), "blue")
        img.save(f, "BMP")

        result = preprocess_image(f)
        assert result is not None
        assert result.suffix == ".jpg"
        assert result.exists()
        assert result.stat().st_size <= MAX_IMAGE_BYTES
        # Original preserved
        assert f.exists()

    def test_converts_webp_to_jpeg(self, tmp_path: Path) -> None:
        """WebP files get converted to JPEG."""
        from PIL import Image

        f = tmp_path / "photo.webp"
        img = Image.new("RGB", (200, 200), "green")
        img.save(f, "WEBP")

        result = preprocess_image(f)
        assert result is not None
        assert result.suffix == ".jpg"
        assert result.exists()

    def test_converts_tiff_to_jpeg(self, tmp_path: Path) -> None:
        """TIFF files get converted to JPEG."""
        from PIL import Image

        f = tmp_path / "scan.tiff"
        img = Image.new("RGB", (300, 300), "white")
        img.save(f, "TIFF")

        result = preprocess_image(f)
        assert result is not None
        assert result.suffix == ".jpg"

    def test_resizes_large_image(self, tmp_path: Path) -> None:
        """Images larger than MAX_IMAGE_DIMENSION are resized."""
        from PIL import Image

        f = tmp_path / "huge.bmp"
        img = Image.new("RGB", (4000, 3000), "red")
        img.save(f, "BMP")

        result = preprocess_image(f)
        assert result is not None

        out_img = Image.open(result)
        assert max(out_img.size) <= MAX_IMAGE_DIMENSION

    def test_converts_rgba_to_rgb(self, tmp_path: Path) -> None:
        """RGBA images are converted to RGB for JPEG compatibility."""
        from PIL import Image

        f = tmp_path / "alpha.webp"
        img = Image.new("RGBA", (100, 100), (255, 0, 0, 128))
        img.save(f, "WEBP")

        result = preprocess_image(f)
        assert result is not None
        out_img = Image.open(result)
        assert out_img.mode == "RGB"

    def test_resizes_oversized_viewable(self, tmp_path: Path) -> None:
        """Large JPEGs over MAX_IMAGE_BYTES get resized."""
        from PIL import Image

        f = tmp_path / "big.jpg"
        # Create a large, detailed image that will exceed 200KB
        img = Image.new("RGB", (3000, 3000))
        # Fill with varied pixel data to prevent compression
        pixels = img.load()
        for x in range(3000):
            for y in range(3000):
                pixels[x, y] = ((x * 7) % 256, (y * 13) % 256, ((x + y) * 3) % 256)
        img.save(f, "JPEG", quality=95)

        if f.stat().st_size <= MAX_IMAGE_BYTES:
            pytest.skip("Test image too small to trigger resize")

        result = preprocess_image(f)
        assert result is not None
        assert result.stat().st_size <= MAX_IMAGE_BYTES

    def test_heic_conversion(self, tmp_path: Path) -> None:
        """HEIC files trigger pillow_heif registration and conversion."""
        from PIL import Image

        f = tmp_path / "photo.heic"
        f.write_bytes(b"fake heic data")

        mock_img = MagicMock()
        mock_img.mode = "RGB"
        mock_img.size = (800, 600)
        mock_img.resize.return_value = mock_img

        # Make save actually create the file
        def fake_save(path, fmt, quality=85):
            real_img = Image.new("RGB", (800, 600), "red")
            real_img.save(path, fmt, quality=quality)
        mock_img.save = fake_save

        mock_heif = MagicMock()
        with patch.dict("sys.modules", {"pillow_heif": mock_heif}), \
             patch("PIL.Image.open", return_value=mock_img):
            result = preprocess_image(f)
            mock_heif.register_heif_opener.assert_called_once()
            assert result is not None
            assert result.suffix == ".jpg"

    def test_returns_none_on_failure(self, tmp_path: Path) -> None:
        """Gracefully returns None when conversion fails."""
        f = tmp_path / "corrupt.webp"
        f.write_bytes(b"not a real image")

        result = preprocess_image(f)
        assert result is None

    def test_output_path_is_sibling(self, tmp_path: Path) -> None:
        """Output file is in the same directory as input."""
        from PIL import Image

        f = tmp_path / "sub" / "photo.bmp"
        f.parent.mkdir()
        img = Image.new("RGB", (100, 100), "blue")
        img.save(f, "BMP")

        result = preprocess_image(f)
        assert result is not None
        assert result.parent == f.parent
