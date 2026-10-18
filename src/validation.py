"""Pure file-validation logic for the Serverless File Processing System.

This module never talks to AWS: it operates on plain bytes and key strings so
every rule can be unit-tested without mocks (PRD 17).

Shared exception contract (reused by ``handler.py`` to serialize failures per
PRD 12): every :class:`ValidationError` exposes

- ``error_code``: machine-readable classification (str),
- ``error_type``: class attribute, exactly ``"ValidationError"``,
- ``str(exc)``: a short human-readable message.
"""

import io
import warnings

from PIL import Image, UnidentifiedImageError

ALLOWED_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "txt"})

JPEG_SIGNATURE = b"\xff\xd8\xff"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_IMAGE_FORMAT_BY_EXTENSION = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG"}

# Includes every frame's canvas; checked before pixel allocation, verification or seeking.
MAX_IMAGE_PIXELS = 16_000_000

VALIDATION_MESSAGES = {
    "ZERO_BYTE": "Object is empty (0 bytes)",
    "SIZE_EXCEEDED": "Object exceeds the configured upload size limit",
    "UNSUPPORTED_EXTENSION": "File extension is not supported",
    "TYPE_MISMATCH": "File content does not match its extension",
    "CORRUPT_IMAGE": "Image integrity verification or decoding failed",
    "IMAGE_DIMENSIONS_EXCEEDED": "Image exceeds the 16000000 total pixel budget",
    "INVALID_ENCODING": "Text content is not valid strict UTF-8",
    "PREFIX_NOT_ALLOWED": "Object key is outside the incoming/ prefix",
}


def check_image_budget(image: Image.Image) -> None:
    """Bound all decoded frame canvases before any image pixel allocation."""
    width, height = image.size
    if width * height * getattr(image, "n_frames", 1) > MAX_IMAGE_PIXELS:
        raise ValidationError(
            "IMAGE_DIMENSIONS_EXCEEDED", VALIDATION_MESSAGES["IMAGE_DIMENSIONS_EXCEEDED"]
        )


class ValidationError(Exception):
    """Permanent validation failure carrying a machine-readable error code."""

    error_type = "ValidationError"

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def extract_extension(object_key: str) -> str | None:
    """Return the lowercased extension of the key's last path segment.

    ``None`` when the file name has no dot or the extension is empty
    (``"incoming/photo."``). Dots in directory names are irrelevant.
    """
    file_name = object_key.rsplit("/", 1)[-1]
    _, dot, extension = file_name.rpartition(".")
    if not dot or not extension:
        return None
    return extension.lower()


def validate_extension(extension: str | None) -> str:
    """Case-insensitively validate *extension* against :data:`ALLOWED_EXTENSIONS`.

    Returns the lowercased extension; raises ``UNSUPPORTED_EXTENSION`` otherwise.
    """
    if not extension or extension.lower() not in ALLOWED_EXTENSIONS:
        shown = repr(extension) if extension else "(no extension)"
        raise ValidationError(
            "UNSUPPORTED_EXTENSION",
            f"Unsupported file extension: {shown}",
        )
    return extension.lower()


def validate_image(file_bytes: bytes, file_type: str) -> None:
    """Validate signature, pixel budget, integrity, then fully decode every frame.

    ``file_type`` is the validated lowercased extension (``jpg``/``jpeg``/``png``).
    A signature that contradicts the claimed type raises ``TYPE_MISMATCH``; a
    matching signature whose image cannot be fully decoded raises
    ``CORRUPT_IMAGE``. Validation either passes fully or raises — images are
    never partially processed.

    Pillow verification closes the image; decode uses a second in-memory open.
    Neither pass downloads the S3 object again. Unexpected errors propagate.
    """
    expected_format = _IMAGE_FORMAT_BY_EXTENSION[file_type]
    signature = JPEG_SIGNATURE if expected_format == "JPEG" else PNG_SIGNATURE
    if not file_bytes.startswith(signature):
        raise ValidationError(
            "TYPE_MISMATCH",
            f"Content signature does not match the '{file_type}' extension",
        )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(file_bytes)) as image:
                check_image_budget(image)
                detected_format = image.format
                image.verify()
            with Image.open(io.BytesIO(file_bytes)) as image:
                check_image_budget(image)
                for frame in range(getattr(image, "n_frames", 1)):
                    image.seek(frame)
                    image.load()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValidationError(
            "IMAGE_DIMENSIONS_EXCEEDED", VALIDATION_MESSAGES["IMAGE_DIMENSIONS_EXCEEDED"]
        ) from exc
    except (UnidentifiedImageError, OSError, SyntaxError, EOFError, ValueError) as exc:
        raise ValidationError(
            "CORRUPT_IMAGE",
            VALIDATION_MESSAGES["CORRUPT_IMAGE"],
        ) from exc
    if detected_format != expected_format:
        raise ValidationError(
            "TYPE_MISMATCH",
            f"Image content is {detected_format}, not {expected_format}",
        )


def decode_text(file_bytes: bytes) -> str:
    """Strictly decode UTF-8 text and strip one leading BOM (U+FEFF).

    Invalid UTF-8 raises ``INVALID_ENCODING``. A BOM-only payload returns
    ``""`` (valid at this layer; zero-byte objects are rejected earlier by
    the handler's size check, PRD 6.3).
    """
    try:
        text = file_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(
            "INVALID_ENCODING",
            "Text content is not valid strict UTF-8",
        ) from exc
    return text.removeprefix("\ufeff")
