"""Regressions for the repository audit: actual handler paths and resource bounds."""

import io
import json
import logging
import random
import struct
import traceback
import tracemalloc
from unittest.mock import Mock

import pytest
from conftest import make_image_bytes
from PIL import Image
from test_handler import CONTEXT, fetch_item, make_event, put_versioned_object, scan_items
from test_handler import handler as handler

import validation
from processors import image as image_processor
from processors.text import extract_metadata


def png_with_bad_checksum():
    data = bytearray(make_image_bytes("PNG"))
    offset = 8
    while offset < len(data):
        size = struct.unpack(">I", data[offset : offset + 4])[0]
        if data[offset + 4 : offset + 8] == b"IDAT":
            data[offset + 8 + size] ^= 1
            return bytes(data)
        offset += size + 12
    raise AssertionError("fixture has no IDAT chunk")


def test_bad_png_checksum_fails_in_handler(handler):
    key = "incoming/checksum.png"
    version = put_versioned_object(handler, key, png_with_bad_checksum())
    handler.lambda_handler(make_event((key, version)), CONTEXT)
    assert fetch_item(handler, key, version)["error"]["error_code"] == "CORRUPT_IMAGE"


def test_oversized_dimensions_rejected_before_decode(monkeypatch):
    fake = Mock()
    fake.__enter__ = Mock(return_value=fake)
    fake.__exit__ = Mock(return_value=False)
    fake.size = (12000, 12000)
    fake.n_frames = 1
    fake.format = "PNG"
    monkeypatch.setattr(validation.Image, "open", Mock(return_value=fake))
    with pytest.raises(validation.ValidationError) as error:
        validation.validate_image(validation.PNG_SIGNATURE, "png")
    assert error.value.error_code == "IMAGE_DIMENSIONS_EXCEEDED"
    fake.load.assert_not_called()


@pytest.mark.parametrize("module", [validation, image_processor])
@pytest.mark.parametrize("failure", [MemoryError, RuntimeError])
def test_unexpected_decoder_errors_are_not_validation_failures(monkeypatch, module, failure):
    def broken_open(*args, **kwargs):
        raise failure("untrusted decoder message")

    monkeypatch.setattr(module.Image, "open", broken_open)
    operation = module.validate_image if module is validation else module.extract_metadata
    with pytest.raises(failure):
        operation(validation.PNG_SIGNATURE, "png")


def test_decoder_memory_error_raises_infrastructure_in_handler(handler, monkeypatch):
    key = "incoming/memory.png"
    version = put_versioned_object(handler, key, make_image_bytes("PNG"))

    def broken_open(*args, **kwargs):
        raise MemoryError("untrusted allocation message")

    monkeypatch.setattr(validation.Image, "open", broken_open)
    with pytest.raises(handler.InfrastructureError):
        handler.lambda_handler(make_event((key, version)), CONTEXT)
    assert fetch_item(handler, key, version)["error"]["error_code"] == "INFRASTRUCTURE"


def test_completed_duplicate_uses_strong_read_and_never_fetches_s3(handler, monkeypatch):
    key = "incoming/duplicate.txt"
    version = put_versioned_object(handler, key, b"already done")
    event = make_event((key, version))
    handler.lambda_handler(event, CONTEXT)
    real_get = handler._processing_table.get_item

    def possibly_stale_read(**kwargs):
        if not kwargs.get("ConsistentRead"):
            return {"Item": {"status": "PROCESSING"}}
        return real_get(**kwargs)

    monkeypatch.setattr(handler._processing_table, "get_item", possibly_stale_read)
    head = Mock(side_effect=AssertionError("completed duplicate fetched S3"))
    monkeypatch.setattr(handler._s3, "head_object", head)
    handler.lambda_handler(event, CONTEXT)
    head.assert_not_called()


def test_logger_does_not_propagate_to_runtime_root(handler):
    root_stream = io.StringIO()
    root_handler = logging.StreamHandler(root_stream)
    root = logging.getLogger()
    root.addHandler(root_handler)
    try:
        handler._log("PROCESSING", "probe", request_id="audit")
    finally:
        root.removeHandler(root_handler)
    assert root_stream.getvalue() == ""


@pytest.mark.parametrize("field", ["bucket", "key", "versionId"])
@pytest.mark.parametrize("value", [123, True, [], {}, None])
def test_invalid_identity_types_write_nothing(handler, field, value):
    record = make_event(("incoming/test.txt", "v1"))["Records"][0]
    if field == "bucket":
        record["s3"]["bucket"]["name"] = value
    else:
        record["s3"]["object"][field] = value
    with pytest.raises(handler.MalformedEventError):
        handler.lambda_handler({"Records": [record]}, CONTEXT)
    assert scan_items(handler) == []


def test_configuration_never_echoes_values_or_chained_value_error(handler, monkeypatch):
    marker = "AUDIT_PRIVATE_VALUE"
    for name in ("MAX_FILE_SIZE_BYTES", "LOG_LEVEL"):
        with monkeypatch.context() as env:
            env.setenv(name, marker)
            with pytest.raises(handler.ConfigurationError) as caught:
                handler._load_config()
            assert marker not in "".join(traceback.format_exception(caught.value))


def test_config_cannot_increase_hard_upload_ceiling(handler, monkeypatch):
    monkeypatch.setenv("MAX_FILE_SIZE_BYTES", "26214401")
    with pytest.raises(handler.ConfigurationError):
        handler._load_config()


def test_untrusted_exception_text_never_leaves_handler(handler, monkeypatch):
    marker = "AUDIT_PRIVATE_PAYLOAD"
    stream = io.StringIO()
    handler.logger.handlers[0].setStream(stream)
    key = "incoming/private.txt"
    version = put_versioned_object(handler, key, b"valid text")

    def broken_metadata(*args):
        try:
            raise ValueError(marker)
        except ValueError as cause:
            raise RuntimeError(marker) from cause

    monkeypatch.setattr(handler.text_processor, "extract_metadata", broken_metadata)
    with pytest.raises(handler.InfrastructureError) as caught:
        handler.lambda_handler(make_event((key, version)), CONTEXT)
    assert marker not in "".join(traceback.format_exception(caught.value))
    assert marker not in stream.getvalue()
    item = fetch_item(handler, key, version)
    assert marker not in json.dumps(item["error"])
    assert "RuntimeError" in stream.getvalue()
    assert "_process_record" in stream.getvalue()


def test_text_counts_match_python_unicode_semantics():
    rng = random.Random(42)
    alphabet = "ab é🙂\t\n\r\v\f\x1c\x1d\x1e\x1f\x85\u2028\u2029\u00a0"
    samples = ["", "\r\n", "a\r\nb\r", "a\x1fb", "\n\n", "\ufeff"]
    samples += ["".join(rng.choices(alphabet, k=rng.randrange(256))) for _ in range(500)]
    for text in samples:
        assert extract_metadata(text) == {
            "characters": len(text), "words": len(text.split()), "lines": len(text.splitlines())
        }


def test_text_statistics_do_not_allocate_per_word_or_line():
    text = "ab \n" * 200000
    tracemalloc.start()
    try:
        result = extract_metadata(text)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result == {"characters": len(text), "words": 200000, "lines": 200000}
    assert peak < 1024 * 1024


def test_animated_png_later_frame_is_decoded(monkeypatch):
    first = Image.new("RGB", (2, 3), "red")
    second = Image.new("RGB", (2, 3), "blue")
    output = io.BytesIO()
    first.save(output, format="PNG", save_all=True, append_images=[second])
    real_load = Image.Image.load

    def failing_later_frame(image, *args, **kwargs):
        if getattr(image, "tell", lambda: 0)() == 1:
            raise OSError("damaged later frame")
        return real_load(image, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "load", failing_later_frame)
    with pytest.raises(validation.ValidationError) as error:
        validation.validate_image(output.getvalue(), "png")
    assert error.value.error_code == "CORRUPT_IMAGE"


@pytest.mark.parametrize("width,accepted", [(4000, True), (4001, False)])
def test_real_png_pixel_boundary(width, accepted):
    data = make_image_bytes("PNG", (width, 4000))
    if accepted:
        validation.validate_image(data, "png")
    else:
        with pytest.raises(validation.ValidationError) as error:
            validation.validate_image(data, "png")
        assert error.value.error_code == "IMAGE_DIMENSIONS_EXCEEDED"


def test_animated_png_budget_counts_all_frame_canvases(monkeypatch):
    output = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(
        output, format="PNG", save_all=True, append_images=[Image.new("RGB", (4, 4), "blue")]
    )
    monkeypatch.setattr(validation, "MAX_IMAGE_PIXELS", 32)
    validation.validate_image(output.getvalue(), "png")
    monkeypatch.setattr(validation, "MAX_IMAGE_PIXELS", 31)
    with pytest.raises(validation.ValidationError) as error:
        validation.validate_image(output.getvalue(), "png")
    assert error.value.error_code == "IMAGE_DIMENSIONS_EXCEEDED"


@pytest.mark.parametrize("record", [None, 1, [], {"s3": []}, {"s3": {"object": []}}])
def test_invalid_record_structure_writes_nothing(handler, record):
    with pytest.raises(handler.MalformedEventError):
        handler.lambda_handler({"Records": [record]}, CONTEXT)
    assert scan_items(handler) == []


def test_non_string_sequencer_is_malformed(handler):
    event = make_event(("incoming/test.txt", "v1"))
    event["Records"][0]["s3"]["object"]["sequencer"] = {"unexpected": "object"}
    with pytest.raises(handler.MalformedEventError):
        handler.lambda_handler(event, CONTEXT)
    assert scan_items(handler) == []
