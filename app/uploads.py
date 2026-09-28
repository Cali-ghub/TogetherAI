"""Safe, private image validation and storage for Together attachments."""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import os
import secrets
import tempfile

from PIL import Image, UnidentifiedImageError

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_ATTACHMENTS = 4
_ALLOWED_FORMATS = {"PNG": ("image/png", ".png"), "JPEG": ("image/jpeg", ".jpg"), "WEBP": ("image/webp", ".webp")}


class UploadValidationError(ValueError):
    """A deliberately generic invalid-image failure."""


@dataclass(frozen=True)
class StoredImage:
    filename: str
    mime_type: str
    width: int
    height: int


def store_image(data: bytes, upload_dir: Path) -> StoredImage:
    """Validate bytes with Pillow, then atomically write an opaque owner-only file."""
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise UploadValidationError("Image must be a PNG, JPEG, or WebP under 10 MiB")
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
        with Image.open(BytesIO(data)) as image:
            image.load()
            image_format = image.format
            width, height = image.size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise UploadValidationError("Image must be a PNG, JPEG, or WebP under 10 MiB") from exc
    if image_format not in _ALLOWED_FORMATS or width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
        raise UploadValidationError("Image must be a PNG, JPEG, or WebP under 10 MiB")

    mime_type, suffix = _ALLOWED_FORMATS[image_format]
    upload_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        upload_dir.chmod(0o700)
    except OSError:
        pass
    filename = f"{secrets.token_urlsafe(24)}{suffix}"
    target = upload_dir / filename
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return StoredImage(filename=filename, mime_type=mime_type, width=width, height=height)


def create_contact_sheet(paths: list[Path], upload_dir: Path) -> Path:
    """Combine up to four images into a numbered sheet for Hermes' single-image CLI input."""
    if not 2 <= len(paths) <= MAX_ATTACHMENTS:
        raise UploadValidationError("Attach between two and four images")
    cell_width, image_height, label_height, gap = 1200, 850, 56, 18
    columns = 2
    rows = (len(paths) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * cell_width + (columns + 1) * gap,
                               rows * (image_height + label_height) + (rows + 1) * gap), "#f6f4ef")
    try:
        from PIL import ImageDraw
        draw = ImageDraw.Draw(sheet)
        for index, path in enumerate(paths):
            with Image.open(path) as source:
                source.load()
                thumb = source.convert("RGB")
            thumb.thumbnail((cell_width, image_height), Image.Resampling.LANCZOS)
            col, row = index % columns, index // columns
            x = gap + col * (cell_width + gap)
            y = gap + row * (image_height + label_height + gap)
            draw.text((x, y + 14), f"IMAGE {index + 1}", fill="#20231e")
            frame_y = y + label_height
            sheet.paste(thumb, (x + (cell_width - thumb.width) // 2,
                                frame_y + (image_height - thumb.height) // 2))
            draw.rectangle((x, frame_y, x + cell_width - 1, frame_y + image_height - 1),
                           outline="#879181", width=2)
        upload_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix="comparison-", suffix=".jpg", dir=upload_dir)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            sheet.save(output, format="JPEG", quality=88, optimize=True)
        return Path(name)
    except BaseException:
        sheet.close()
        raise
    finally:
        sheet.close()
