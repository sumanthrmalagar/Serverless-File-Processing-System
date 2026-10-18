"""Unit tests for the pure validation module (no AWS, no network)."""

import pytest
from conftest import make_image_bytes

from validation import (
    ValidationError,
    decode_text,
    extract_extension,
    validate_extension,
    validate_image,
)


def assert_validation_error(exc_info: pytest.ExceptionInfo, error_code: str) -> None:
    error = exc_info.value
    assert isinstance(error, ValidationError)
    assert error.error_code == error_code
    assert error.error_type == "ValidationError"
    assert str(error)


@pytest.mark.parametrize(
    ("object_key", "expected"),
    [
        ("incoming/photo.JPG", "jpg"),
        ("incoming/noext", None),
        ("incoming/archive.tar.gz", "gz"),
        ("incoming/photo.", None),
        ("incoming/dir.name/file", None),
    ],
)
def test_extract_extension(object_key: str, expected: str | None) -> None:
    assert extract_extension(object_key) == expected


@pytest.mark.parametrize("extension", ["jpg", "jpeg", "png", "txt", "JPG", "PNG", "TXT"])
def test_validate_extension_accepts_allowlisted(extension: str) -> None:
    assert validate_extension(extension) == extension.lower()


@pytest.mark.parametrize("extension", ["pdf", None])
def test_validate_extension_rejects_non_allowlisted(extension: str | None) -> None:
    with pytest.raises(ValidationError) as exc_info:
        validate_extension(extension)
    assert_validation_error(exc_info, "UNSUPPORTED_EXTENSION")
    if extension is None:
        assert "(no extension)" in str(exc_info.value)
    else:
        assert extension in str(exc_info.value)


def test_validate_image_accepts_jpeg_as_jpg_and_jpeg() -> None:
    jpeg_bytes = make_image_bytes("JPEG")
    validate_image(jpeg_bytes, "jpg")
    validate_image(jpeg_bytes, "jpeg")


def test_validate_image_accepts_png() -> None:
    validate_image(make_image_bytes("PNG"), "png")


def test_validate_image_png_claimed_as_jpg_is_type_mismatch() -> None:
    with pytest.raises(ValidationError) as exc_info:
        validate_image(make_image_bytes("PNG"), "jpg")
    assert_validation_error(exc_info, "TYPE_MISMATCH")


def test_validate_image_jpeg_claimed_as_png_is_type_mismatch() -> None:
    with pytest.raises(ValidationError) as exc_info:
        validate_image(make_image_bytes("JPEG"), "png")
    assert_validation_error(exc_info, "TYPE_MISMATCH")


def test_validate_image_truncated_jpeg_is_corrupt() -> None:
    truncated = make_image_bytes("JPEG")[:100]
    with pytest.raises(ValidationError) as exc_info:
        validate_image(truncated, "jpg")
    assert_validation_error(exc_info, "CORRUPT_IMAGE")


def test_validate_image_truncated_png_is_corrupt() -> None:
    truncated = make_image_bytes("PNG")[:20]
    with pytest.raises(ValidationError) as exc_info:
        validate_image(truncated, "png")
    assert_validation_error(exc_info, "CORRUPT_IMAGE")


def test_validate_image_non_image_bytes_is_type_mismatch() -> None:
    with pytest.raises(ValidationError) as exc_info:
        validate_image(b"not an image at all", "jpg")
    assert_validation_error(exc_info, "TYPE_MISMATCH")


def test_decode_text_roundtrips_unicode() -> None:
    assert decode_text("héllo wörld".encode()) == "héllo wörld"


def test_decode_text_rejects_invalid_utf8() -> None:
    with pytest.raises(ValidationError) as exc_info:
        decode_text(b"\xff\xfe\x00")
    assert_validation_error(exc_info, "INVALID_ENCODING")


def test_decode_text_strips_leading_bom() -> None:
    assert decode_text(b"\xef\xbb\xbfhello") == "hello"


def test_decode_text_bom_only_returns_empty_string() -> None:
    assert decode_text("\ufeff".encode()) == ""


def test_decode_text_empty_bytes_returns_empty_string() -> None:
    assert decode_text(b"") == ""
