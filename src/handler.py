"""AWS orchestration for the Serverless File Processing System (PRD §5–§13).

handler.py owns everything AWS-related: event parsing, S3 and DynamoDB calls,
the conditional write protocol, error categorization, structured logging, and
configuration. Pure file logic lives in validation.py, checksum.py, and
processors/ and never talks to AWS.

Failure model (PRD §11): permanent validation failures become FAILED records
and the batch continues; transient/infrastructure failures are logged with
stack traces (CloudWatch only), get a guarded FAILED attempt when a record
exists, and re-raise so Lambda's async retries and the SQS on-failure
destination engage.
"""

import hashlib
import json
import logging
import os
import time
from datetime import UTC, datetime
from urllib.parse import unquote_plus

import boto3
from botocore.exceptions import ClientError

import checksum
import validation
from processors import image as image_processor
from processors import text as text_processor

DEFAULT_MAX_FILE_SIZE_BYTES = 26214400

_TERMINAL_CONDITION = "attribute_exists(processing_id) AND #status <> :completed"

_MAX_ERROR_JSON_LENGTH = 1024


class InfrastructureError(Exception):
    """Transient/infrastructure failure (PRD §11)."""

    error_type = "InfrastructureError"
    error_code = "INFRASTRUCTURE"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class MalformedEventError(Exception):
    """Event or record that cannot be safely interpreted (PRD §11)."""

    error_type = "MalformedEventError"
    error_code = "MALFORMED_EVENT"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class ConfigurationError(InfrastructureError):
    """Invalid environment configuration; raised at import (PRD §5)."""


def _load_config() -> tuple[str, int, str]:
    """Read and validate the environment contract (PRD §5).

    Missing TABLE_NAME, MAX_FILE_SIZE_BYTES outside 1..26214400, or an
    invalid LOG_LEVEL raise ConfigurationError at import time (loud failure:
    async retries, then the SQS failure destination).
    """
    table_name = os.environ.get("TABLE_NAME", "").strip()
    if not table_name:
        raise ConfigurationError(
            "TABLE_NAME environment variable is required and must be non-empty"
        )

    raw_max_file_size = os.environ.get("MAX_FILE_SIZE_BYTES")
    if raw_max_file_size is None:
        max_file_size_bytes = DEFAULT_MAX_FILE_SIZE_BYTES
    else:
        try:
            max_file_size_bytes = int(raw_max_file_size)
        except ValueError:
            raise ConfigurationError(
                "MAX_FILE_SIZE_BYTES must be an integer between 1 and 26214400"
            ) from None
        if not 0 < max_file_size_bytes <= DEFAULT_MAX_FILE_SIZE_BYTES:
            raise ConfigurationError(
                "MAX_FILE_SIZE_BYTES must be an integer between 1 and 26214400"
            )

    log_level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    if not isinstance(logging.getLevelName(log_level_name), int):
        raise ConfigurationError(
            "LOG_LEVEL must be a valid Python logging level name"
        )

    return table_name, max_file_size_bytes, log_level_name


TABLE_NAME, MAX_FILE_SIZE_BYTES, LOG_LEVEL = _load_config()


class _JsonFormatter(logging.Formatter):
    """One JSON object per line (PRD §13); never logs secrets or file contents."""

    _EVENT_FIELDS = (
        "status",
        "request_id",
        "event_id",
        "processing_id",
        "bucket",
        "object_key",
        "version_id",
        "file_type",
        "file_size",
        "processing_duration",
        "error_type",
        "error_code",
    )

    def formatException(self, exc_info) -> str:
        """Keep diagnostic frames, omitting exception text, source lines and locals."""
        exception_type, _, tb = exc_info
        lines = ["Traceback (most recent call last):"]
        while tb is not None:
            code = tb.tb_frame.f_code
            lines.append(
                f'  File "{os.path.basename(code.co_filename)}", '
                f"line {tb.tb_lineno}, in {code.co_name}"
            )
            tb = tb.tb_next
        lines.append(f"{exception_type.__name__}: exception message omitted")
        return "\n".join(lines)

    def format(self, record: logging.LogRecord) -> str:
        entry: dict = {"level": record.levelname, "message": record.getMessage()}
        for field in self._EVENT_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                entry[field] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def _configure_logger() -> logging.Logger:
    log = logging.getLogger("sfps")
    log.setLevel(logging.getLevelName(LOG_LEVEL))
    log.propagate = False
    log.handlers.clear()  # keep module reloads idempotent under pytest
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(_JsonFormatter())
    log.addHandler(stream_handler)
    return log


logger = _configure_logger()

_s3 = boto3.client("s3")
_dynamodb = boto3.resource("dynamodb")
_processing_table = _dynamodb.Table(TABLE_NAME)


def _utc_timestamp() -> str:
    """UTC ISO-8601 at second precision ending in Z (AD-011)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


def _processing_id(bucket: str, object_key: str, version_id: str) -> str:
    """Deterministic processing identity (PRD §6.2): SHA-256 of canonical JSON."""
    identity = json.dumps(
        {"bucket": bucket, "object_key": object_key, "version_id": version_id},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _error_entry(error: Exception) -> dict:
    """Serialize an exception into the PRD §12 shape {error_code, error_type, message}.

    Known validation codes use allowlisted messages. Other exceptions become
    INFRASTRUCTURE / InfrastructureError with a fixed message; raw exception
    strings are never stored. The serialized JSON is capped at 1024 characters
    by truncating only ``message``; classification is never truncated.
    """
    if isinstance(error, validation.ValidationError) and error.error_code in (
        validation.VALIDATION_MESSAGES
    ):
        entry = {
            "error_code": error.error_code,
            "error_type": "ValidationError",
            "message": validation.VALIDATION_MESSAGES[error.error_code],
        }
    else:
        entry = {
            "error_code": InfrastructureError.error_code,
            "error_type": InfrastructureError.error_type,
            "message": "Infrastructure or unexpected processing failure; see diagnostic logs",
        }
    if len(json.dumps(entry, sort_keys=True)) <= _MAX_ERROR_JSON_LENGTH:
        return entry
    message = entry["message"]
    low, high = 0, len(message)
    while low < high:
        middle = (low + high + 1) // 2
        entry["message"] = message[:middle]
        if len(json.dumps(entry, sort_keys=True)) <= _MAX_ERROR_JSON_LENGTH:
            low = middle
        else:
            high = middle - 1
    entry["message"] = message[:low]
    return entry


def _is_conditional_check_failed(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _log(status: str, message: str, *, request_id: str, **fields: object) -> None:
    extra: dict = {"status": status, "request_id": request_id}
    extra.update({key: value for key, value in fields.items() if value is not None})
    logger.info(message, extra=extra)


def _log_malformed(request_id: str, error: MalformedEventError) -> None:
    logger.error(
        "Malformed S3 event; raising (no DynamoDB record written)",
        extra={
            "request_id": request_id,
            "error_type": error.error_type,
            "error_code": error.error_code,
        },
    )


def _parse_records(event: dict) -> list[dict]:
    records = event.get("Records") if isinstance(event, dict) else None
    if not isinstance(records, list) or not records:
        raise MalformedEventError("Event has no Records array")
    return records


def _parse_record(record: dict) -> dict:
    if not isinstance(record, dict):
        raise MalformedEventError("Record must be an object")
    try:
        s3_section = record["s3"]
        bucket = s3_section["bucket"]["name"]
        raw_key = s3_section["object"]["key"]
        version_id = s3_section["object"]["versionId"]
    except (KeyError, TypeError):
        raise MalformedEventError("Record is missing required S3 identity fields") from None

    if not isinstance(bucket, str) or not bucket.strip():
        raise MalformedEventError("Record is missing the bucket name")
    if not isinstance(raw_key, str) or not raw_key:
        raise MalformedEventError("Record is missing the object key")
    if not isinstance(version_id, str) or not version_id.strip() or version_id == "null":
        raise MalformedEventError("Record is missing the object version_id")

    event_id = s3_section["object"].get("sequencer")
    if event_id is not None and not isinstance(event_id, str):
        raise MalformedEventError("Record sequencer must be a string when present")

    return {
        "bucket": bucket,
        "object_key": unquote_plus(raw_key),
        "version_id": version_id,
        "event_id": event_id,
    }


def _claim_processing_record(
    processing_id: str,
    bucket: str,
    object_key: str,
    version_id: str,
    file_name: str,
    file_type: str | None,
) -> bool:
    """Claim a new record; return False when it is already COMPLETED (PRD §10).

    The claim is a conditional PutItem. On ConditionalCheckFailedException the
    existing record is inspected: only COMPLETED is skipped. PROCESSING/FAILED
    records are re-attempted without overwriting created_at.
    """
    item = {
        "processing_id": processing_id,
        "bucket": bucket,
        "object_key": object_key,
        "version_id": version_id,
        "file_name": file_name,
        "status": "PROCESSING",
        "created_at": _utc_timestamp(),
    }
    if file_type is not None:
        item["file_type"] = file_type
    try:
        _processing_table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(processing_id)",
        )
        return True
    except ClientError as exc:
        if not _is_conditional_check_failed(exc):
            raise

    existing = _processing_table.get_item(
        Key={"processing_id": processing_id}, ConsistentRead=True
    ).get("Item")
    return not (existing is not None and existing.get("status") == "COMPLETED")


def _head_object_size(bucket: str, object_key: str, version_id: str) -> int:
    """Authoritative object size from a version-pinned HeadObject (PRD §6.3)."""
    response = _s3.head_object(Bucket=bucket, Key=object_key, VersionId=version_id)
    return int(response["ContentLength"])


def _get_object_bytes(bucket: str, object_key: str, version_id: str) -> bytes:
    """Fetch the version-pinned object body exactly once (PRD §6.3)."""
    response = _s3.get_object(Bucket=bucket, Key=object_key, VersionId=version_id)
    body = response["Body"]
    try:
        return body.read()
    finally:
        body.close()


def _write_completed(
    processing_id: str,
    file_size: int,
    sha256: str,
    metadata: dict,
    file_type: str,
) -> None:
    """Terminal COMPLETED transition, guarded so COMPLETED is never overwritten."""
    try:
        _processing_table.update_item(
            Key={"processing_id": processing_id},
            UpdateExpression=(
                "SET #status = :completed, processed_at = :now, file_size = :size, "
                "sha256 = :sha256, metadata = :metadata, file_type = :file_type "
                "REMOVE #error"
            ),
            ConditionExpression=_TERMINAL_CONDITION,
            ExpressionAttributeNames={"#status": "status", "#error": "error"},
            ExpressionAttributeValues={
                ":completed": "COMPLETED",
                ":now": _utc_timestamp(),
                ":size": file_size,
                ":sha256": sha256,
                ":metadata": metadata,
                ":file_type": file_type,
            },
        )
    except ClientError as exc:
        if not _is_conditional_check_failed(exc):
            raise
        logger.info(
            "Terminal COMPLETED skipped: record already completed by another invocation",
            extra={"status": "COMPLETED", "processing_id": processing_id},
        )


def _write_failed(
    processing_id: str,
    error: Exception,
    *,
    file_size: int | None,
    file_type: str | None,
) -> None:
    """Terminal FAILED transition, guarded so COMPLETED is never overwritten."""
    update_expression = "SET #status = :failed, processed_at = :now, #error = :error"
    expression_values: dict = {
        ":completed": "COMPLETED",
        ":failed": "FAILED",
        ":now": _utc_timestamp(),
        ":error": _error_entry(error),
    }
    if file_size is not None:
        update_expression += ", file_size = :size"
        expression_values[":size"] = file_size
    if file_type is not None:
        update_expression += ", file_type = :file_type"
        expression_values[":file_type"] = file_type
    try:
        _processing_table.update_item(
            Key={"processing_id": processing_id},
            UpdateExpression=update_expression,
            ConditionExpression=_TERMINAL_CONDITION,
            ExpressionAttributeNames={"#status": "status", "#error": "error"},
            ExpressionAttributeValues=expression_values,
        )
    except ClientError as exc:
        if not _is_conditional_check_failed(exc):
            raise
        logger.info(
            "Terminal FAILED skipped: record already completed by another invocation",
            extra={"status": "FAILED", "processing_id": processing_id},
        )


def _process_record(record: dict, context) -> None:
    """Run the PRD §6 per-record pipeline in order."""
    request_id = context.aws_request_id
    try:
        parsed = _parse_record(record)
    except MalformedEventError as exc:
        _log_malformed(request_id, exc)
        raise

    bucket = parsed["bucket"]
    object_key = parsed["object_key"]
    version_id = parsed["version_id"]
    event_id = parsed["event_id"]

    file_name = object_key.rsplit("/", 1)[-1]
    file_type = validation.extract_extension(object_key)
    processing_id = _processing_id(bucket, object_key, version_id)

    record_exists = False
    started_at: float | None = None
    file_size: int | None = None
    try:
        if not _claim_processing_record(
            processing_id, bucket, object_key, version_id, file_name, file_type
        ):
            _log(
                "COMPLETED",
                "Duplicate S3 event skipped: record already COMPLETED",
                request_id=request_id,
                event_id=event_id,
                processing_id=processing_id,
                bucket=bucket,
                object_key=object_key,
                version_id=version_id,
                file_type=file_type,
            )
            return

        record_exists = True
        started_at = time.monotonic()
        _log(
            "PROCESSING",
            "Processing started",
            request_id=request_id,
            event_id=event_id,
            processing_id=processing_id,
            bucket=bucket,
            object_key=object_key,
            version_id=version_id,
            file_type=file_type,
        )

        if not object_key.startswith("incoming/"):
            raise validation.ValidationError(
                "PREFIX_NOT_ALLOWED",
                f"Object key is outside the incoming/ prefix: {object_key}",
            )

        file_size = _head_object_size(bucket, object_key, version_id)

        if file_size == 0:
            raise validation.ValidationError("ZERO_BYTE", "Object is empty (0 bytes)")
        if file_size > MAX_FILE_SIZE_BYTES:
            raise validation.ValidationError(
                "SIZE_EXCEEDED",
                f"Object is {file_size} bytes; the limit is {MAX_FILE_SIZE_BYTES} bytes",
            )

        file_type = validation.validate_extension(file_type)

        file_bytes = _get_object_bytes(bucket, object_key, version_id)

        if file_type == "txt":
            metadata = text_processor.extract_metadata(validation.decode_text(file_bytes))
        else:
            validation.validate_image(file_bytes, file_type)
            metadata = image_processor.extract_metadata(file_bytes, file_type)

        sha256 = checksum.sha256_hex(file_bytes)

        _write_completed(processing_id, file_size, sha256, metadata, file_type)
        _log(
            "COMPLETED",
            "Processing completed",
            request_id=request_id,
            event_id=event_id,
            processing_id=processing_id,
            bucket=bucket,
            object_key=object_key,
            version_id=version_id,
            file_type=file_type,
            file_size=file_size,
            processing_duration=_elapsed_ms(started_at),
        )
    except validation.ValidationError as exc:
        try:
            _write_failed(processing_id, exc, file_size=file_size, file_type=file_type)
        except Exception:
            logger.exception(
                "Could not record FAILED state for a permanent validation failure; "
                "raising for retry",
                extra={
                    "status": "FAILED",
                    "request_id": request_id,
                    "processing_id": processing_id,
                    "object_key": object_key,
                    "version_id": version_id,
                    "error_type": "InfrastructureError",
                    "error_code": "INFRASTRUCTURE",
                },
            )
            raise
        _log(
            "FAILED",
            "Processing failed (permanent validation failure)",
            request_id=request_id,
            event_id=event_id,
            processing_id=processing_id,
            bucket=bucket,
            object_key=object_key,
            version_id=version_id,
            file_type=file_type,
            file_size=file_size,
            processing_duration=_elapsed_ms(started_at),
            error_type=exc.error_type,
            error_code=exc.error_code,
        )
    except Exception as exc:
        error_entry = _error_entry(exc)
        logger.exception(
            "Unhandled exception while processing record; classified as infrastructure failure",
            extra={
                "status": "FAILED",
                "request_id": request_id,
                "event_id": event_id,
                "processing_id": processing_id,
                "bucket": bucket,
                "object_key": object_key,
                "version_id": version_id,
                "file_type": file_type,
                "file_size": file_size,
                "processing_duration": (
                    _elapsed_ms(started_at) if started_at is not None else None
                ),
                "error_type": error_entry["error_type"],
                "error_code": error_entry["error_code"],
            },
        )
        if record_exists:
            try:
                _write_failed(processing_id, exc, file_size=file_size, file_type=file_type)
            except Exception:
                logger.exception(
                    "Secondary failure: could not record FAILED state; "
                    "raising the original exception",
                    extra={
                        "status": "FAILED",
                        "request_id": request_id,
                        "processing_id": processing_id,
                        "object_key": object_key,
                        "version_id": version_id,
                        "error_type": "InfrastructureError",
                        "error_code": "INFRASTRUCTURE",
                    },
                )
        raise


def lambda_handler(event: dict, context) -> None:
    """Entry point: process every S3 record sequentially (PRD §6.1).

    Permanent validation failures are recorded and the batch continues;
    malformed events and transient failures raise (PRD §11).
    """
    try:
        records = _parse_records(event)
    except MalformedEventError as exc:
        _log_malformed(context.aws_request_id, exc)
        raise
    try:
        for record in records:
            _process_record(record, context)
    except MalformedEventError:
        raise
    except Exception:
        # Lambda also prints uncaught exception text and includes it in destination
        # responsePayload. Do not let SDK/decoder/file-content messages escape there.
        raise InfrastructureError(
            "File processing failed; see sanitized CloudWatch diagnostic logs"
        ) from None
