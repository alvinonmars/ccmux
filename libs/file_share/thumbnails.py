"""Thumbnail generation for image shares using Pillow."""
from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"})


def generate_thumbnails(
    source_dir: Path,
    thumb_dir: Path,
    size: tuple[int, int] = (300, 200),
    quality: int = 70,
) -> int:
    """Generate thumbnails for all images in source_dir. Return count created.

    Idempotent — skips files that already have a thumbnail.
    """
    source_dir = Path(source_dir)
    thumb_dir = Path(thumb_dir)
    thumb_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    for img_path in sorted(source_dir.iterdir()):
        if not img_path.is_file():
            continue
        if img_path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue

        thumb_path = thumb_dir / f"{img_path.stem}.jpg"
        if thumb_path.exists():
            continue

        try:
            with Image.open(img_path) as img:
                img.thumbnail(size, Image.LANCZOS)
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                img.save(thumb_path, "JPEG", quality=quality)
                count += 1
        except Exception:
            logger.warning("Failed to create thumbnail for %s", img_path, exc_info=True)

    return count
