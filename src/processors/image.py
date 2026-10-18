"""Pure image metadata extraction (PRD 7). No AWS calls, in-memory only."""

import io

from PIL import Image, UnidentifiedImageError

from validation import ValidationError, check_image_budget


def extract_metadata(file_bytes: bytes, file_type: str) -> dict:
    """Return ``{"width", "height", "image_format"}`` for validated image bytes.

    ``file_type`` is the validated lowercased extension (``jpg``/``jpeg``/``png``).
    ``image_format`` is the Pillow-detected format string, exactly ``"JPEG"`` or
    ``"PNG"``.

    Validation already verified and decoded every frame. Only expected invalid
    image errors become CORRUPT_IMAGE here; unexpected failures propagate.
    """
    try:
        with Image.open(io.BytesIO(file_bytes)) as image:
            check_image_budget(image)
            return {
                "width": image.width,
                "height": image.height,
                "image_format": image.format,
            }
    except (UnidentifiedImageError, OSError, SyntaxError, EOFError, ValueError) as exc:
        raise ValidationError(
            "CORRUPT_IMAGE",
            f"Image metadata could not be extracted ({exc.__class__.__name__})",
        ) from exc
