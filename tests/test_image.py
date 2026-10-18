"""Unit tests for the image metadata processor (no AWS, no network)."""

import pytest
from conftest import make_image_bytes

from processors.image import extract_metadata
from validation import ValidationError


def test_extract_metadata_png_dimensions_and_format() -> None:
    metadata = extract_metadata(make_image_bytes("PNG", (2, 3)), "png")
    assert metadata == {"width": 2, "height": 3, "image_format": "PNG"}


def test_extract_metadata_jpeg_dimensions_and_format() -> None:
    metadata = extract_metadata(make_image_bytes("JPEG", (7, 5)), "jpg")
    assert metadata == {"width": 7, "height": 5, "image_format": "JPEG"}


def test_extract_metadata_accepts_jpeg_extension() -> None:
    metadata = extract_metadata(make_image_bytes("JPEG", (3, 2)), "jpeg")
    assert metadata["image_format"] == "JPEG"


def test_extract_metadata_returns_exact_key_set() -> None:
    metadata = extract_metadata(make_image_bytes("PNG", (2, 3)), "png")
    assert set(metadata) == {"width", "height", "image_format"}


def test_extract_metadata_invalid_bytes_is_corrupt_image() -> None:
    with pytest.raises(ValidationError) as exc_info:
        extract_metadata(b"not an image at all", "jpg")
    assert exc_info.value.error_code == "CORRUPT_IMAGE"
    assert exc_info.value.error_type == "ValidationError"
