"""Handler tests: moto-mocked S3 + DynamoDB, synthetic S3 events, no network."""

import hashlib
import importlib
import json
import logging
import sys
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from conftest import make_image_bytes
from moto import mock_aws

import handler as handler_module
from validation import ValidationError

BUCKET = "sfps-incoming-000000000000"
TABLE_NAME = "sfps-processing"
DEFAULT_MAX_FILE_SIZE_BYTES = 26214400
CONTEXT = SimpleNamespace(aws_request_id="test-request-id")


def _create_bucket_and_table(module):
    module._s3.create_bucket(Bucket=BUCKET)
    module._s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
    module._dynamodb.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName": "processing_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "processing_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def handler(monkeypatch, caplog):
    monkeypatch.setenv("TABLE_NAME", TABLE_NAME)
    monkeypatch.delenv("MAX_FILE_SIZE_BYTES", raising=False)
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    with mock_aws():
        module = importlib.reload(handler_module)
        _create_bucket_and_table(module)
        module.logger.addHandler(caplog.handler)
        yield module
        module.logger.removeHandler(caplog.handler)


@pytest.fixture
def handler_small_max(monkeypatch, caplog):
    monkeypatch.setenv("TABLE_NAME", TABLE_NAME)
    monkeypatch.setenv("MAX_FILE_SIZE_BYTES", "1024")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    with mock_aws():
        module = importlib.reload(handler_module)
        _create_bucket_and_table(module)
        module.logger.addHandler(caplog.handler)
        yield module
        module.logger.removeHandler(caplog.handler)


def record_for(key, version_id, bucket=BUCKET):
    return {
        "s3": {
            "bucket": {"name": bucket},
            "object": {
                "key": key,
                "size": 0,
                "versionId": version_id,
                "sequencer": "00A0000000000000",
            },
        }
    }


def make_event(*entries):
    return {"Records": [record_for(key, version_id) for key, version_id in entries]}


def put_versioned_object(module, key, body):
    return module._s3.put_object(Bucket=BUCKET, Key=key, Body=body)["VersionId"]


def fetch_item(module, key, version_id):
    processing_id = module._processing_id(BUCKET, key, version_id)
    return module._processing_table.get_item(Key={"processing_id": processing_id})["Item"]


def scan_items(module):
    return module._processing_table.scan()["Items"]


def exploding_image_processor(*args, **kwargs):
    raise RuntimeError("processor exploded")


def _reload_with_env(monkeypatch, *, table_name, max_file_size=None, log_level=None):
    if table_name is None:
        monkeypatch.delenv("TABLE_NAME", raising=False)
    else:
        monkeypatch.setenv("TABLE_NAME", table_name)
    for name, value in (("MAX_FILE_SIZE_BYTES", max_file_size), ("LOG_LEVEL", log_level)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def _reload_expecting_configuration_error(monkeypatch, **env):
    _reload_with_env(monkeypatch, **env)
    with pytest.raises(Exception) as exc_info:
        importlib.reload(handler_module)
    error = exc_info.value
    assert isinstance(error, handler_module.ConfigurationError)
    assert isinstance(error, handler_module.InfrastructureError)
    return error


# --- Configuration (PRD §5) ---------------------------------------------------


def test_missing_table_name_raises_configuration_error(monkeypatch):
    error = _reload_expecting_configuration_error(monkeypatch, table_name=None)
    assert error.error_code == "INFRASTRUCTURE"


def test_empty_table_name_raises_configuration_error(monkeypatch):
    _reload_expecting_configuration_error(monkeypatch, table_name="   ")


@pytest.mark.parametrize("value", ["abc", "0", "-5"])
def test_invalid_max_file_size_raises_configuration_error(monkeypatch, value):
    _reload_expecting_configuration_error(monkeypatch, table_name=TABLE_NAME, max_file_size=value)


def test_invalid_log_level_raises_configuration_error(monkeypatch):
    _reload_expecting_configuration_error(monkeypatch, table_name=TABLE_NAME, log_level="NOPE")


def test_config_defaults_are_applied(monkeypatch):
    _reload_with_env(monkeypatch, table_name=TABLE_NAME)
    module = importlib.reload(handler_module)
    assert module.MAX_FILE_SIZE_BYTES == DEFAULT_MAX_FILE_SIZE_BYTES
    assert module.LOG_LEVEL == "INFO"


# --- Processing identity (PRD §6.2) -------------------------------------------


def test_processing_id_is_deterministic_64_char_hex():
    first = handler_module._processing_id("bucket", "incoming/photo.jpg", "v1")
    second = handler_module._processing_id("bucket", "incoming/photo.jpg", "v1")
    assert first == second
    assert len(first) == 64
    assert all(character in "0123456789abcdef" for character in first)


def test_processing_id_changes_with_any_input():
    reference = handler_module._processing_id("bucket", "incoming/photo.jpg", "v1")
    assert handler_module._processing_id("other", "incoming/photo.jpg", "v1") != reference
    assert handler_module._processing_id("bucket", "incoming/other.jpg", "v1") != reference
    assert handler_module._processing_id("bucket", "incoming/photo.jpg", "v2") != reference


def test_processing_id_locked_regression_digest():
    digest = handler_module._processing_id("bucket-1", "incoming/photo.jpg", "version-abc")
    assert digest == "cdfa791d6d6f97dbfaf7b2f6682a33b2ce0dffec39b1b39af31c3f72ff4e42bd"


# --- Event parsing (PRD §6.1) -------------------------------------------------


@pytest.mark.parametrize(
    ("raw_key", "decoded_key"),
    [
        ("incoming/hello%20world.txt", "incoming/hello world.txt"),
        ("incoming/a+b.txt", "incoming/a b.txt"),
        ("incoming/a%2Bb.txt", "incoming/a+b.txt"),
    ],
)
def test_url_encoded_keys_are_decoded_with_unquote_plus(handler, raw_key, decoded_key):
    version_id = put_versioned_object(handler, decoded_key, b"hello")
    handler.lambda_handler(make_event((raw_key, version_id)), CONTEXT)
    item = fetch_item(handler, decoded_key, version_id)
    assert item["object_key"] == decoded_key
    assert item["status"] == "COMPLETED"


def test_missing_version_id_raises_and_writes_nothing(handler):
    key = "incoming/file.txt"
    version_id = put_versioned_object(handler, key, b"hello")
    record = record_for(key, version_id)
    del record["s3"]["object"]["versionId"]
    with pytest.raises(handler.MalformedEventError):
        handler.lambda_handler({"Records": [record]}, CONTEXT)
    assert scan_items(handler) == []


@pytest.mark.parametrize("version_id", ["", "null"])
def test_empty_or_null_version_id_raises_and_writes_nothing(handler, version_id):
    with pytest.raises(handler.MalformedEventError):
        handler.lambda_handler(make_event(("incoming/file.txt", version_id)), CONTEXT)
    assert scan_items(handler) == []


def test_missing_records_raises(handler):
    with pytest.raises(handler.MalformedEventError):
        handler.lambda_handler({}, CONTEXT)


def test_empty_records_raises(handler):
    with pytest.raises(handler.MalformedEventError):
        handler.lambda_handler({"Records": []}, CONTEXT)


def test_missing_bucket_name_raises(handler):
    record = record_for("incoming/file.txt", "v1")
    record["s3"]["bucket"] = {}
    with pytest.raises(handler.MalformedEventError):
        handler.lambda_handler({"Records": [record]}, CONTEXT)
    assert scan_items(handler) == []


def test_missing_object_key_raises(handler):
    record = record_for("incoming/file.txt", "v1")
    del record["s3"]["object"]["key"]
    with pytest.raises(handler.MalformedEventError):
        handler.lambda_handler({"Records": [record]}, CONTEXT)
    assert scan_items(handler) == []


# --- Claim protocol and idempotency (PRD §10) ---------------------------------


def test_duplicate_event_skips_completed_and_changes_nothing(handler):
    key = "incoming/duplicate.txt"
    version_id = put_versioned_object(handler, key, b"duplicate me")
    event = make_event((key, version_id))

    handler.lambda_handler(event, CONTEXT)
    item_before = fetch_item(handler, key, version_id)
    handler.lambda_handler(event, CONTEXT)
    item_after = fetch_item(handler, key, version_id)

    assert item_before["status"] == "COMPLETED"
    assert item_after == item_before


def test_processing_record_is_re_attempted_without_overwriting_created_at(handler):
    key = "incoming/retry.txt"
    version_id = put_versioned_object(handler, key, b"retry me")
    processing_id = handler._processing_id(BUCKET, key, version_id)
    handler._processing_table.put_item(
        Item={
            "processing_id": processing_id,
            "bucket": BUCKET,
            "object_key": key,
            "version_id": version_id,
            "file_name": "retry.txt",
            "file_type": "txt",
            "status": "PROCESSING",
            "created_at": "2026-01-01T00:00:00Z",
        }
    )

    handler.lambda_handler(make_event((key, version_id)), CONTEXT)

    item = fetch_item(handler, key, version_id)
    assert item["status"] == "COMPLETED"
    assert item["created_at"] == "2026-01-01T00:00:00Z"


def test_terminal_conditional_check_failure_is_not_raised(handler, monkeypatch):
    key = "incoming/race.txt"
    version_id = put_versioned_object(handler, key, b"race me")
    processing_id = handler._processing_id(BUCKET, key, version_id)
    real_sha256_hex = handler.checksum.sha256_hex

    def racing_sha256_hex(data):
        handler._processing_table.update_item(
            Key={"processing_id": processing_id},
            UpdateExpression="SET #status = :completed, processed_at = :now",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":completed": "COMPLETED",
                ":now": "2026-02-02T00:00:00Z",
            },
        )
        return real_sha256_hex(data)

    monkeypatch.setattr(handler.checksum, "sha256_hex", racing_sha256_hex)

    handler.lambda_handler(make_event((key, version_id)), CONTEXT)

    item = fetch_item(handler, key, version_id)
    assert item["status"] == "COMPLETED"
    assert item["processed_at"] == "2026-02-02T00:00:00Z"


# --- Prefix enforcement (PRD §6.1) --------------------------------------------


def test_key_outside_incoming_prefix_fails(handler):
    key = "uploads/file.txt"
    version_id = put_versioned_object(handler, key, b"nope")
    handler.lambda_handler(make_event((key, version_id)), CONTEXT)

    item = fetch_item(handler, key, version_id)
    assert item["status"] == "FAILED"
    assert item["error"]["error_code"] == "PREFIX_NOT_ALLOWED"
    assert item["error"]["error_type"] == "ValidationError"


# --- Success pipeline (PRD §6, §7, §8) ----------------------------------------


def test_jpeg_upload_produces_completed_record(handler):
    key = "incoming/photo.jpg"
    body = make_image_bytes("JPEG", (7, 5))
    version_id = put_versioned_object(handler, key, body)
    handler.lambda_handler(make_event((key, version_id)), CONTEXT)

    item = fetch_item(handler, key, version_id)
    assert item["status"] == "COMPLETED"
    assert item["bucket"] == BUCKET
    assert item["object_key"] == key
    assert item["version_id"] == version_id
    assert item["file_name"] == "photo.jpg"
    assert item["file_type"] == "jpg"
    assert item["file_size"] == len(body)
    assert item["sha256"] == hashlib.sha256(body).hexdigest()
    assert item["metadata"] == {"width": 7, "height": 5, "image_format": "JPEG"}
    assert item["created_at"].endswith("Z")
    assert item["processed_at"].endswith("Z")
    assert "error" not in item


def test_png_upload_produces_completed_record(handler):
    key = "incoming/photo.png"
    body = make_image_bytes("PNG", (2, 3))
    version_id = put_versioned_object(handler, key, body)
    handler.lambda_handler(make_event((key, version_id)), CONTEXT)

    item = fetch_item(handler, key, version_id)
    assert item["status"] == "COMPLETED"
    assert item["file_type"] == "png"
    assert item["metadata"] == {"width": 2, "height": 3, "image_format": "PNG"}


def test_txt_upload_produces_completed_record(handler):
    key = "incoming/notes.txt"
    body = b"hello world\nsecond line\n"
    version_id = put_versioned_object(handler, key, body)
    handler.lambda_handler(make_event((key, version_id)), CONTEXT)

    item = fetch_item(handler, key, version_id)
    assert item["status"] == "COMPLETED"
    assert item["metadata"] == {"characters": 24, "words": 4, "lines": 2}
    assert item["sha256"] == hashlib.sha256(body).hexdigest()


def test_uppercase_extension_is_accepted(handler):
    key = "incoming/PHOTO.JPG"
    body = make_image_bytes("JPEG")
    version_id = put_versioned_object(handler, key, body)
    handler.lambda_handler(make_event((key, version_id)), CONTEXT)

    item = fetch_item(handler, key, version_id)
    assert item["status"] == "COMPLETED"
    assert item["file_type"] == "jpg"
    assert item["file_name"] == "PHOTO.JPG"


@pytest.mark.parametrize(
    ("key", "body"),
    [
        ("incoming/2026/photo.jpg", make_image_bytes("JPEG")),
        ("incoming/users/demo/file.txt", b"nested text"),
    ],
)
def test_nested_keys_are_processed(handler, key, body):
    version_id = put_versioned_object(handler, key, body)
    handler.lambda_handler(make_event((key, version_id)), CONTEXT)

    item = fetch_item(handler, key, version_id)
    assert item["status"] == "COMPLETED"
    assert item["file_name"] == key.rsplit("/", 1)[-1]


def test_multi_record_batch_handles_failure_and_success(handler):
    bad_key = "incoming/archive.zip"
    good_key = "incoming/ok.txt"
    bad_version = put_versioned_object(handler, bad_key, b"PK\x03\x04 fake zip")
    good_version = put_versioned_object(handler, good_key, b"fine")

    handler.lambda_handler(make_event((bad_key, bad_version), (good_key, good_version)), CONTEXT)

    bad_item = fetch_item(handler, bad_key, bad_version)
    good_item = fetch_item(handler, good_key, good_version)
    assert bad_item["status"] == "FAILED"
    assert bad_item["error"]["error_code"] == "UNSUPPORTED_EXTENSION"
    assert good_item["status"] == "COMPLETED"


def test_invalid_utf8_text_fails_with_invalid_encoding(handler):
    key = "incoming/bad.txt"
    version_id = put_versioned_object(handler, key, b"\xff\xfe\x00")
    handler.lambda_handler(make_event((key, version_id)), CONTEXT)

    item = fetch_item(handler, key, version_id)
    assert item["status"] == "FAILED"
    assert set(item["error"]) == {"error_code", "error_type", "message"}
    assert item["error"]["error_code"] == "INVALID_ENCODING"
    assert item["error"]["error_type"] == "ValidationError"
    assert item["error"]["message"]
    assert "sha256" not in item
    assert "metadata" not in item


# --- Pre-download failures (PRD §6.3, §6.4) -----------------------------------


def test_unsupported_extension_fails_without_downloading(handler, monkeypatch):
    key = "incoming/archive.pdf"
    version_id = put_versioned_object(handler, key, b"%PDF-1.4 not supported")
    get_object_calls = []
    real_get_object = handler._s3.get_object

    def spy_get_object(**kwargs):
        get_object_calls.append(kwargs)
        return real_get_object(**kwargs)

    monkeypatch.setattr(handler._s3, "get_object", spy_get_object)

    handler.lambda_handler(make_event((key, version_id)), CONTEXT)

    assert get_object_calls == []
    item = fetch_item(handler, key, version_id)
    assert item["status"] == "FAILED"
    assert item["error"]["error_code"] == "UNSUPPORTED_EXTENSION"
    assert item["file_size"] == len(b"%PDF-1.4 not supported")


def test_key_without_extension_fails_unsupported(handler):
    key = "incoming/noextension"
    version_id = put_versioned_object(handler, key, b"data")
    handler.lambda_handler(make_event((key, version_id)), CONTEXT)

    item = fetch_item(handler, key, version_id)
    assert item["status"] == "FAILED"
    assert item["error"]["error_code"] == "UNSUPPORTED_EXTENSION"


def test_zero_byte_object_fails_zero_byte(handler):
    key = "incoming/empty.txt"
    version_id = put_versioned_object(handler, key, b"")
    handler.lambda_handler(make_event((key, version_id)), CONTEXT)

    item = fetch_item(handler, key, version_id)
    assert item["status"] == "FAILED"
    assert item["error"]["error_code"] == "ZERO_BYTE"
    assert item["file_size"] == 0


def test_size_over_configured_max_fails(handler_small_max):
    key = "incoming/too-big.txt"
    body = b"x" * 1025
    version_id = put_versioned_object(handler_small_max, key, body)
    handler_small_max.lambda_handler(make_event((key, version_id)), CONTEXT)

    item = fetch_item(handler_small_max, key, version_id)
    assert item["status"] == "FAILED"
    assert item["error"]["error_code"] == "SIZE_EXCEEDED"
    assert item["file_size"] == 1025


def test_size_exactly_at_configured_max_is_accepted(handler_small_max):
    key = "incoming/exact.txt"
    body = b"x" * 1024
    version_id = put_versioned_object(handler_small_max, key, body)
    handler_small_max.lambda_handler(make_event((key, version_id)), CONTEXT)

    item = fetch_item(handler_small_max, key, version_id)
    assert item["status"] == "COMPLETED"
    assert item["file_size"] == 1024


def test_default_limit_exactly_25_mib_is_accepted(handler):
    key = "incoming/limit.txt"
    body = b"x" * DEFAULT_MAX_FILE_SIZE_BYTES
    version_id = put_versioned_object(handler, key, body)
    handler.lambda_handler(make_event((key, version_id)), CONTEXT)

    item = fetch_item(handler, key, version_id)
    assert item["status"] == "COMPLETED"
    assert item["file_size"] == DEFAULT_MAX_FILE_SIZE_BYTES


def test_default_limit_25_mib_plus_one_is_rejected(handler):
    key = "incoming/over-limit.txt"
    body = b"x" * (DEFAULT_MAX_FILE_SIZE_BYTES + 1)
    version_id = put_versioned_object(handler, key, body)
    handler.lambda_handler(make_event((key, version_id)), CONTEXT)

    item = fetch_item(handler, key, version_id)
    assert item["status"] == "FAILED"
    assert item["error"]["error_code"] == "SIZE_EXCEEDED"


# --- Version pinning (PRD §6.2, §6.3) ------------------------------------------


def test_version_pinned_reads_use_event_version(handler, monkeypatch):
    key = "incoming/pinned.txt"
    first_version = put_versioned_object(handler, key, b"version one")
    second_version = put_versioned_object(handler, key, b"version two")
    assert first_version != second_version

    head_calls = []
    get_calls = []
    real_head_object = handler._s3.head_object
    real_get_object = handler._s3.get_object

    def spy_head_object(**kwargs):
        head_calls.append(kwargs)
        return real_head_object(**kwargs)

    def spy_get_object(**kwargs):
        get_calls.append(kwargs)
        return real_get_object(**kwargs)

    monkeypatch.setattr(handler._s3, "head_object", spy_head_object)
    monkeypatch.setattr(handler._s3, "get_object", spy_get_object)

    handler.lambda_handler(make_event((key, first_version)), CONTEXT)

    assert head_calls and head_calls[0]["VersionId"] == first_version
    assert get_calls and get_calls[0]["VersionId"] == first_version
    item = fetch_item(handler, key, first_version)
    assert item["sha256"] == hashlib.sha256(b"version one").hexdigest()
    assert item["file_size"] == len(b"version one")


# --- Content failures end-to-end (PRD §6.4) -----------------------------------


def test_png_content_claimed_as_jpg_fails_type_mismatch(handler):
    key = "incoming/disguised.jpg"
    version_id = put_versioned_object(handler, key, make_image_bytes("PNG"))
    handler.lambda_handler(make_event((key, version_id)), CONTEXT)

    item = fetch_item(handler, key, version_id)
    assert item["status"] == "FAILED"
    assert item["error"]["error_code"] == "TYPE_MISMATCH"
    assert item["error"]["error_type"] == "ValidationError"
    assert "sha256" not in item
    assert "metadata" not in item


def test_truncated_jpeg_fails_corrupt_image(handler):
    key = "incoming/truncated.jpg"
    version_id = put_versioned_object(handler, key, make_image_bytes("JPEG")[:100])
    handler.lambda_handler(make_event((key, version_id)), CONTEXT)

    item = fetch_item(handler, key, version_id)
    assert item["status"] == "FAILED"
    assert item["error"]["error_code"] == "CORRUPT_IMAGE"
    assert item["error"]["error_type"] == "ValidationError"


def test_truncated_png_fails_corrupt_image(handler):
    key = "incoming/truncated.png"
    version_id = put_versioned_object(handler, key, make_image_bytes("PNG")[:20])
    handler.lambda_handler(make_event((key, version_id)), CONTEXT)

    item = fetch_item(handler, key, version_id)
    assert item["status"] == "FAILED"
    assert item["error"]["error_code"] == "CORRUPT_IMAGE"


# --- Failure model: malformed logging, transient classification ---------------


def test_malformed_event_is_logged_before_raising(handler, caplog):
    with caplog.at_level(logging.ERROR):
        with pytest.raises(handler.MalformedEventError):
            handler.lambda_handler(make_event(("incoming/file.txt", "null")), CONTEXT)

    records = [record for record in caplog.records if record.name == "sfps"]
    assert any(getattr(record, "error_code", None) == "MALFORMED_EVENT" for record in records)


def test_claim_dynamodb_failure_raises_and_writes_nothing(handler, monkeypatch):
    key = "incoming/claim-boom.txt"
    version_id = put_versioned_object(handler, key, b"data")

    def broken_put_item(**kwargs):
        raise ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "claim failed"}}, "PutItem"
        )

    monkeypatch.setattr(handler._processing_table, "put_item", broken_put_item)

    with pytest.raises(handler.InfrastructureError):
        handler.lambda_handler(make_event((key, version_id)), CONTEXT)

    assert scan_items(handler) == []


def test_head_object_service_error_is_transient_and_marks_failed(handler, monkeypatch):
    key = "incoming/head-boom.txt"
    version_id = put_versioned_object(handler, key, b"head boom")

    def broken_head_object(**kwargs):
        raise ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "access denied"}}, "HeadObject"
        )

    monkeypatch.setattr(handler._s3, "head_object", broken_head_object)

    with pytest.raises(handler.InfrastructureError):
        handler.lambda_handler(make_event((key, version_id)), CONTEXT)

    item = fetch_item(handler, key, version_id)
    assert item["status"] == "FAILED"
    assert item["error"]["error_code"] == "INFRASTRUCTURE"
    assert item["error"]["error_type"] == "InfrastructureError"


def test_terminal_completed_failure_writes_failed_state_and_raises(handler, monkeypatch):
    key = "incoming/terminal-boom.txt"
    version_id = put_versioned_object(handler, key, b"terminal boom")

    def broken_write_completed(*args, **kwargs):
        raise ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "terminal update failed"}},
            "UpdateItem",
        )

    monkeypatch.setattr(handler, "_write_completed", broken_write_completed)

    with pytest.raises(handler.InfrastructureError):
        handler.lambda_handler(make_event((key, version_id)), CONTEXT)

    item = fetch_item(handler, key, version_id)
    assert item["status"] == "FAILED"
    assert item["error"]["error_code"] == "INFRASTRUCTURE"
    assert item["error"]["error_type"] == "InfrastructureError"


def test_secondary_failure_is_logged_and_safe_exception_raised(handler, monkeypatch, caplog):
    key = "incoming/all-down.txt"
    version_id = put_versioned_object(handler, key, b"all down")

    def broken_update_item(**kwargs):
        raise ClientError(
            {"Error": {"Code": "ServiceUnavailable", "Message": "dynamodb down"}}, "UpdateItem"
        )

    monkeypatch.setattr(handler._processing_table, "update_item", broken_update_item)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(handler.InfrastructureError):
            handler.lambda_handler(make_event((key, version_id)), CONTEXT)

    item = fetch_item(handler, key, version_id)
    assert item["status"] == "PROCESSING"
    assert "Secondary failure" in caplog.text


def test_unexpected_exception_marks_record_failed_and_raises(handler, monkeypatch, caplog):
    key = "incoming/boom.jpg"
    version_id = put_versioned_object(handler, key, make_image_bytes("JPEG"))

    monkeypatch.setattr(handler.image_processor, "extract_metadata", exploding_image_processor)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(handler.InfrastructureError):
            handler.lambda_handler(make_event((key, version_id)), CONTEXT)

    item = fetch_item(handler, key, version_id)
    assert item["status"] == "FAILED"
    assert item["error"]["error_type"] == "InfrastructureError"
    assert item["error"]["error_code"] == "INFRASTRUCTURE"
    assert "processor exploded" not in item["error"]["message"]
    failure_records = [
        record
        for record in caplog.records
        if record.name == "sfps" and getattr(record, "error_code", None) == "INFRASTRUCTURE"
    ]
    assert failure_records
    assert any(record.exc_info for record in failure_records)


def test_multi_record_transient_stops_batch_and_keeps_prior_results(handler, monkeypatch):
    first_version = put_versioned_object(handler, "incoming/first.txt", b"first")
    boom_version = put_versioned_object(handler, "incoming/boom.jpg", make_image_bytes("JPEG"))
    later_version = put_versioned_object(handler, "incoming/later.txt", b"later")

    monkeypatch.setattr(handler.image_processor, "extract_metadata", exploding_image_processor)

    event = make_event(
        ("incoming/first.txt", first_version),
        ("incoming/boom.jpg", boom_version),
        ("incoming/later.txt", later_version),
    )
    with pytest.raises(handler.InfrastructureError):
        handler.lambda_handler(event, CONTEXT)

    assert fetch_item(handler, "incoming/first.txt", first_version)["status"] == "COMPLETED"
    boom_item = fetch_item(handler, "incoming/boom.jpg", boom_version)
    assert boom_item["status"] == "FAILED"
    assert boom_item["error"]["error_code"] == "INFRASTRUCTURE"

    later_processing_id = handler._processing_id(BUCKET, "incoming/later.txt", later_version)
    later_item = handler._processing_table.get_item(Key={"processing_id": later_processing_id}).get(
        "Item"
    )
    assert later_item is None


# --- Error truncation (PRD §12) -----------------------------------------------


def test_error_entry_replaces_long_validation_message():
    error = ValidationError("TYPE_MISMATCH", "x" * 5000)
    entry = handler_module._error_entry(error)

    assert entry["error_code"] == "TYPE_MISMATCH"
    assert entry["error_type"] == "ValidationError"
    assert entry["message"]
    assert len(json.dumps(entry, sort_keys=True)) <= 1024


def test_error_entry_replaces_long_untrusted_message_with_escapes():
    error = RuntimeError(('bad "quote" \\ backslash wrapped in a long string ' * 500).strip())
    entry = handler_module._error_entry(error)

    assert entry["error_code"] == "INFRASTRUCTURE"
    assert entry["error_type"] == "InfrastructureError"
    assert entry["message"]
    assert len(json.dumps(entry, sort_keys=True)) <= 1024


# --- Structured logging contract (PRD §13) ------------------------------------


def test_json_formatter_emits_one_line_valid_json():
    formatter = handler_module._JsonFormatter()
    record = logging.LogRecord(
        name="sfps",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Processing started",
        args=(),
        exc_info=None,
    )
    record.status = "PROCESSING"
    record.request_id = "req-1"
    record.object_key = "incoming/x.txt"

    output = formatter.format(record)

    assert "\n" not in output
    parsed = json.loads(output)
    assert parsed["message"] == "Processing started"
    assert parsed["level"] == "INFO"
    assert parsed["status"] == "PROCESSING"
    assert parsed["request_id"] == "req-1"
    assert parsed["object_key"] == "incoming/x.txt"


def test_json_formatter_keeps_stack_trace_on_one_line():
    formatter = handler_module._JsonFormatter()
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        name="sfps",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Unhandled exception",
        args=(),
        exc_info=exc_info,
    )

    output = formatter.format(record)

    assert "\n" not in output
    parsed = json.loads(output)
    assert "RuntimeError: exception message omitted" in parsed["exception"]
    assert "RuntimeError: boom" not in parsed["exception"]


def test_record_cycle_logs_start_terminal_and_duplicate_lines(handler, caplog):
    key = "incoming/logged.txt"
    version_id = put_versioned_object(handler, key, b"logged")
    event = make_event((key, version_id))

    with caplog.at_level(logging.INFO):
        handler.lambda_handler(event, CONTEXT)
        handler.lambda_handler(event, CONTEXT)

    records = [record for record in caplog.records if record.name == "sfps"]
    start_lines = [record for record in records if getattr(record, "status", None) == "PROCESSING"]
    completed_lines = [
        record for record in records if record.getMessage() == "Processing completed"
    ]
    duplicate_lines = [
        record for record in records if record.getMessage().startswith("Duplicate S3 event skipped")
    ]

    assert len(start_lines) == 1
    assert len(completed_lines) == 1
    assert len(duplicate_lines) == 1
    terminal = completed_lines[0]
    assert terminal.object_key == key
    assert terminal.version_id == version_id
    assert isinstance(terminal.processing_duration, int)
    assert terminal.processing_duration >= 0
