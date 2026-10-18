# Serverless File Processing System (SFPS) — V1

An event-driven AWS file-processing pipeline. Uploading a file under the `incoming/` prefix
of a private, versioned S3 bucket asynchronously invokes a single AWS Lambda function, which
validates the file (extension allowlist, content signature, decodability), extracts metadata
(image dimensions/format or text character/word/line counts), computes a SHA-256 checksum
over the original version-pinned bytes, and stores exactly one deterministic processing
record per S3 object version in DynamoDB. Structured JSON logs go to CloudWatch Logs
(30-day retention). Permanent validation failures are recorded as `FAILED` and do not
themselves trigger retries; transient failures raise, exhaust Lambda's async retries, and an
invocation envelope containing the original event under `requestPayload` lands in the
`sfps-failed-events` SQS queue.

**Deployment is manual in V1.** Every AWS resource is created by hand in the AWS Management
Console (no SAM/CloudFormation/Terraform, no API Gateway, no CI/CD). The deployment steps,
policies, packaging commands, and verification checks are documented below.

---

## Architecture

```text
File Upload
    ↓
Amazon S3  (bucket: sfps-incoming-<aws-account-id>, prefix: incoming/)
    ↓  s3:ObjectCreated:*  (asynchronous invocation)
AWS Lambda  (sfps-processor — Python 3.13, arm64, 512 MB, 30 s)
    ├── Validate (extension → signature → decode)
    ├── Detect type + extract metadata
    └── SHA-256 (over version-pinned original bytes)
    ↓
DynamoDB  (sfps-processing — one record per S3 object version)

Lambda → CloudWatch Logs  (/aws/lambda/sfps-processor, 30-day retention)

Unrecoverable async failure (after retries) → SQS  (sfps-failed-events)
```

| Service | Responsibility |
|---|---|
| S3 | Private, versioned source-file storage and the upload trigger |
| Lambda | File validation and processing |
| DynamoDB | Processing status and metadata |
| CloudWatch | Structured logs and diagnostics |
| SQS | Capture of failed asynchronous Lambda events |
| IAM | Least-privilege execution role |

### Resource names

| Resource | Name |
|---|---|
| S3 bucket | `sfps-incoming-<aws-account-id>` (globally unique; lowercase) |
| DynamoDB table | `sfps-processing` |
| SQS queue | `sfps-failed-events` (standard) |
| IAM role | `sfps-lambda-role` |
| Lambda function | `sfps-processor` |
| CloudWatch log group | `/aws/lambda/sfps-processor` |

### Deployment values (fill in after deploying)

Record the chosen region. Every resource lives in this one region.

| Value | Yours |
|---|---|
| Region | `<record the chosen region here>` |
| AWS account ID | `<aws-account-id>` |
| Bucket | `sfps-incoming-<aws-account-id>` |
| SQS queue URL | `https://sqs.<region>.amazonaws.com/<aws-account-id>/sfps-failed-events` |
| SQS queue ARN | `arn:aws:sqs:<region>:<aws-account-id>:sfps-failed-events` |
| DynamoDB table ARN | `arn:aws:dynamodb:<region>:<aws-account-id>:table/sfps-processing` |
| Lambda function ARN | `arn:aws:lambda:<region>:<aws-account-id>:function:sfps-processor` |
| Log group | `/aws/lambda/sfps-processor` |

---

## Prerequisites

- AWS account with permissions to create S3, DynamoDB, SQS, IAM, Lambda, and CloudWatch
  resources (an admin-level principal is sufficient for V1).
- **Python 3.13.x** (matches the Lambda runtime; the build step downloads Python 3.13
  `cp313` Linux arm64 wheels).
- **AWS CLI** (optional): used in a few verification commands and for uploading the
  deployment package; all resource creation can be done in the console.
- Internet access for `pip` (dependency and wheel downloads).
- Build host: **Windows 11 + PowerShell 5.1** (the packaging commands below are
  PowerShell; on Linux/macOS replace activation with `source .venv/bin/activate` and adapt
  the copy/zip commands).

---

## Repository setup (local)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

Run tests and lint (no AWS credentials or network needed — AWS is mocked with `moto`):

```powershell
python -m pytest
python -m ruff check src tests
```

Repository layout:

```text
src/                  Lambda source (flat zip layout)
  handler.py          AWS orchestration: config, logging, protocol, pipeline
  validation.py       Pure content validation (no AWS)
  checksum.py         SHA-256 helper (no AWS)
  processors/         Image and text metadata extraction (no AWS)
tests/                pytest suite (moto-mocked; offline)
requirements.txt      Exact pins for every runtime dependency, including botocore
requirements-dev.txt  Dev pins (pytest, moto, ruff)
pyproject.toml        Ruff + pytest configuration
build/                Generated deployment package contents (git-ignored)
function.zip          Generated deployment package (git-ignored)
```

---

## Configuration

The Lambda function reads three environment variables (configured in the Lambda console;
there are no `.env` files in this project):

| Variable | Required | Default | Validation |
|---|---|---|---|
| `TABLE_NAME` | Yes | — | Missing or empty → configuration error raised at init |
| `MAX_FILE_SIZE_BYTES` | No | `26214400` (25 MiB) | Integer from `1` through `26214400`; otherwise configuration error |
| `LOG_LEVEL` | No | `INFO` | Must be a valid Python logging level name, else configuration error |

Configuration errors are intentionally loud: they raise at init, exhaust Lambda's async
retries, and the original event reaches the SQS failure destination under `requestPayload`.

---

## Resource and diagnostic contracts

- Uploads have a **25 MiB hard ceiling**. `MAX_FILE_SIZE_BYTES` may lower that ceiling.
- Images have a **16,000,000 total pixel budget**: width × height × frame count.
  Check this before allocating pixels; larger images fail `IMAGE_DIMENSIONS_EXCEEDED`.
  Pillow verifies integrity, then reopens the same bytes and decodes every frame.
  Checksum-damaged PNGs fail `CORRUPT_IMAGE`; unexpected decoder errors raise.
- Text statistics preserve Python Unicode split/splitlines results using iterators,
  without allocating lists proportional to the number of words or lines.
- Error messages persisted to DynamoDB come from a fixed allowlist. Unexpected
  failures log diagnostic frames and exception class, then raise a fixed-message
  `InfrastructureError` from `None` at the Lambda boundary. Raw exception messages,
  chains, source lines and locals are omitted. The application logger does not propagate
  to the runtime root logger; Lambda platform START/END/REPORT lines are separate.
- Bucket/key/version fields must be nonempty strings; malformed identities write no
  record for that identity. Earlier records in a batch may already have completed.

## Deployment (manual, V1)

The steps below create every resource and configuration value. Use one region everywhere.

**1. Choose the region.** Record it in the Deployment values table above.

**2. Create the S3 bucket.** S3 console → **General purpose buckets** → **Create bucket**:
name `sfps-incoming-<aws-account-id>`, your region.

**3. Configure the bucket** (on the same create page):

- **Block Public Access settings:** keep **Block all public access** enabled (all four).
- **Bucket Versioning:** **Enable** (required — events carry a `version_id`).
- **Default encryption:** **SSE-S3** (Amazon S3 managed keys). S3 Bucket Keys apply to SSE-KMS, not SSE-S3.

**4. Add the TLS-only bucket policy.** Bucket → **Permissions** → **Bucket policy** →
**Edit**. This policy denies every non-TLS request:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyNonTLSRequests",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:*",
      "Resource": [
        "arn:aws:s3:::sfps-incoming-<aws-account-id>",
        "arn:aws:s3:::sfps-incoming-<aws-account-id>/*"
      ],
      "Condition": {
        "Bool": {
          "aws:SecureTransport": "false"
        }
      }
    }
  ]
}
```

**5. Add the lifecycle rule** expiring noncurrent versions after 30 days. Bucket →
**Management** → **Create lifecycle rule**: name it (e.g. `expire-noncurrent-30d`), scope
"all objects", action **Permanently delete noncurrent versions of objects**, set **Days
after objects become noncurrent: 30**.

**6. Create the DynamoDB table.** DynamoDB console → **Create table**: name
`sfps-processing`; partition key `processing_id` (**String**); no sort key; under
**Customize settings** choose capacity mode **On-demand** and encryption **Owned by Amazon
DynamoDB**; leave PITR disabled; no secondary indexes.

**7. Create the SQS queue.** SQS console → **Create queue**: type **Standard**, name
`sfps-failed-events`; **Message retention period 14 days**; **Visibility timeout 30
seconds**; keep the default **SSE-SQS** server-side encryption; dead-letter queue
**Disabled**. Copy the queue ARN. SQS deletes a message only when a consumer explicitly
calls `DeleteMessage`; there is nothing to configure. No consumer runs in V1; the queue
exists solely to capture exhausted async failures.

**8. Create the IAM role `sfps-lambda-role`.** IAM console → **Roles** → **Create role** →
trusted entity **AWS service** → use case **Lambda**. Attach no managed policies. Create
the role, then add this **exact scoped inline policy** (replace `<region>`,
`<aws-account-id>`, and the bucket name):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3ReadPinnedObjects",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:GetObjectVersion"
      ],
      "Resource": "arn:aws:s3:::sfps-incoming-<aws-account-id>/incoming/*"
    },
    {
      "Sid": "DynamoProcessingTable",
      "Effect": "Allow",
      "Action": [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem"
      ],
      "Resource": "arn:aws:dynamodb:<region>:<aws-account-id>:table/sfps-processing"
    },
    {
      "Sid": "FailureQueueSend",
      "Effect": "Allow",
      "Action": [
        "sqs:SendMessage"
      ],
      "Resource": "arn:aws:sqs:<region>:<aws-account-id>:sfps-failed-events"
    },
    {
      "Sid": "FunctionLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:<region>:<aws-account-id>:log-group:/aws/lambda/sfps-processor:*"
    }
  ]
}
```

Notes: `HeadObject` is authorized by `s3:GetObject`/`s3:GetObjectVersion` (no separate
action). `logs:CreateLogGroup` is intentionally absent — step 14 pre-creates the log group.

**9. Create the Lambda function.** Lambda console → **Create function** → **Author from
scratch**: name `sfps-processor`; runtime **Python 3.13**; architecture **arm64**
(changeable later under **Code → Runtime settings → Edit** with compatible code);
permissions → **Use an existing role** → `sfps-lambda-role`. Afterwards, on the function
page:

- **Configuration → General configuration → Edit:** Memory **512 MB**, Timeout **0 min
  30 sec**.
- **Code → Runtime settings → Edit:** Handler `handler.lambda_handler`.
- **Configuration → Environment variables → Edit:** `TABLE_NAME` = `sfps-processing`
  (optionally set `MAX_FILE_SIZE_BYTES` / `LOG_LEVEL` explicitly; see Configuration).

**10. Build and upload the deployment package.** See "Deployment package" below, then
Lambda → **Code** → **Upload from** → **.zip file** → select `function.zip`.

**11. Configure asynchronous invocation.**
**Configuration → Asynchronous invocation → Edit:** **Retry attempts 2** (leave Maximum
age of event at the default 6 hours). Then **Function overview → Add destination:**
Source **Asynchronous invocation**, Condition **On failure**, Destination type **SQS**,
destination `sfps-failed-events` ARN. (This is Lambda's `DestinationConfig.OnFailure` —
not a classic dead-letter queue.)

**12. Add the S3 invoke permission** (principal `s3.amazonaws.com`, scoped to the bucket).
The Lambda console trigger in step 13 creates this automatically; verify it under
**Configuration → Permissions → Resource-based policy statements** and confirm it is
restricted by `AWS:SourceArn` (bucket) and `AWS:SourceAccount`. If it is missing the
scoping, replace it via AWS CLI:

```powershell
aws lambda remove-permission --function-name sfps-processor --statement-id <existing-id> --region <region>
aws lambda add-permission --function-name sfps-processor --statement-id s3invoke --action lambda:InvokeFunction --principal s3.amazonaws.com --source-arn arn:aws:s3:::sfps-incoming-<aws-account-id> --source-account <aws-account-id> --region <region>
```

**13. Add the S3 event notification.** Lambda → **Function overview → Add trigger** →
source **S3**: bucket `sfps-incoming-<aws-account-id>`; **Event types: All object create
events** (`s3:ObjectCreated:*`); **Prefix `incoming/`** (exact, lowercase); **Suffix
empty** (no suffix filter — unsupported files must reach Lambda and become `FAILED`
records); accept the recursive-invocation acknowledgment; **Add**. Verify the notification
on the bucket under **Properties → Event notifications**. New notification configurations
can take up to ~5 minutes to activate.

**14. Set CloudWatch log retention.** Because the role cannot create log groups, first
create it: CloudWatch → **Log groups** → **Create log group** →
`/aws/lambda/sfps-processor` (set **Retention: 30 days** if offered on the create page).
If not offered, select the group afterwards → **Actions → Edit retention setting** →
**30 days**. CLI equivalent:

```powershell
aws logs create-log-group --log-group-name /aws/lambda/sfps-processor --region <region>
aws logs put-retention-policy --log-group-name /aws/lambda/sfps-processor --retention-in-days 30 --region <region>
```

---

`MaximumRetryAttempts=2` means up to two **function-error** retries after the initial
attempt. Throttling and Lambda system errors have separate queue retries up to the
maximum event age; duplicate delivery remains possible. An SQS destination receives
an invocation envelope, with the entire original event in `requestPayload`.
See [AWS retry behavior](https://docs.aws.amazon.com/lambda/latest/dg/invocation-async-error-handling.html)
and [destination format](https://docs.aws.amazon.com/lambda/latest/dg/invocation-async-retain-records.html).

## Deployment package

The Lambda deployment package is built on the Windows host and uploaded manually. It must
contain **Linux arm64** wheels (Python 3.13, `cp313`) — never locally installed Windows
wheels:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
Remove-Item -Recurse -Force build -ErrorAction SilentlyContinue
python -m pip install --python-version 3.13 --implementation cp --abi cp313 --platform manylinux_2_28_aarch64 --only-binary=:all: --no-compile --target build -r requirements.txt
Get-ChildItem -Path src -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
Copy-Item -Path src\* -Destination build\ -Recurse -Force
Remove-Item -Force function.zip -ErrorAction SilentlyContinue
python -c "import zipfile, os; z = zipfile.ZipFile('function.zip','w',zipfile.ZIP_DEFLATED); [z.write(os.path.join(dp,f), os.path.relpath(os.path.join(dp,f),'build').replace(os.sep,'/')) for dp,dn,fn in os.walk('build') for f in fn]; z.close()"
python -c "import zipfile; names = zipfile.ZipFile('function.zip').namelist(); assert 'handler.py' in names; assert all('\\' not in n for n in names); assert all(not n.startswith('./') for n in names); print(len(names), 'entries OK')"
aws lambda update-function-code --function-name sfps-processor --zip-file fileb://function.zip --region <region>
```

Rules and caveats:

- `requirements.txt` locks all runtime transitives. Verify every packaged dependency
  version and source file against the repository. Zip timestamps/order may differ;
  matching archive sizes alone do not prove reproducibility.

- Copy the **contents** of `build/` (flat layout) so `handler.py` sits at the **zip root**,
  matching the handler `handler.lambda_handler`.
- The zip must use **forward-slash** entry paths. **Never use PowerShell 5.1
  `Compress-Archive`** — it writes backslash separators into zip entry names and Lambda
  cannot unpack the result. The Python `zipfile` command above is safe; 7-Zip or
  `tar -a -c -f function.zip -C build .` also work if the entry check passes.
- The platform tag is `manylinux_2_28_aarch64` (supported by Lambda's Amazon Linux 2023
  runtime). The older `manylinux2014_aarch64` tag does not resolve for the pinned Pillow
  12.3.0 on cp313.
- `--no-compile` keeps `__pycache__`/`.pyc` out of the package.
- The zip must stay under the **50 MB** direct-upload limit (verified build: ~22 MB). If it
  ever exceeds it, upload the zip to S3 and reference the object key instead.

---

## Acceptance walkthrough

You need: `sample.jpg`, `sample.png`, `sample.txt` (any content), and one unsupported file
(e.g. `sample.pdf`).

1. **Upload** via the S3 console: create folder `incoming/`, upload `sample.jpg`,
   `sample.png`, `sample.pdf` inside it; create `incoming/2026/` and upload `sample.txt`
   there. CLI alternative:
   ```powershell
   aws s3 cp sample.jpg s3://sfps-incoming-<aws-account-id>/incoming/sample.jpg
   aws s3 cp sample.png s3://sfps-incoming-<aws-account-id>/incoming/sample.png
   aws s3 cp sample.txt s3://sfps-incoming-<aws-account-id>/incoming/2026/sample.txt
   aws s3 cp sample.pdf s3://sfps-incoming-<aws-account-id>/incoming/sample.pdf
   ```
2. **Verify DynamoDB records** (DynamoDB → Tables → `sfps-processing` → Explore table
   items → Scan). Expected:

   | `object_key` | `status` | Details |
   |---|---|---|
   | `incoming/sample.jpg` | `COMPLETED` | `metadata` = width/height/`image_format`; `sha256` present; `file_size` matches |
   | `incoming/sample.png` | `COMPLETED` | image metadata + `sha256` |
   | `incoming/2026/sample.txt` | `COMPLETED` | `metadata` = characters/words/lines |
   | `incoming/sample.pdf` | `FAILED` | `error.error_code` = `UNSUPPORTED_EXTENSION` |

   Note: console folder creation also creates zero-byte folder-marker objects
   (`incoming/`, `incoming/2026/`) whose records end `FAILED` with `ZERO_BYTE` — expected
   behavior. Optional hash cross-check: `(Get-FileHash .\sample.jpg -Algorithm SHA256).Hash.ToLower()`
   must equal the record's `sha256`.
3. **Verify CloudWatch logs** (`/aws/lambda/sfps-processor`): each invocation stream
   contains one JSON object per line, with a `PROCESSING` start line and a
   `COMPLETED`/`FAILED` terminal line carrying `request_id`, `object_key`, `version_id`,
   `processing_duration`, and error fields on failures. Confirm the group shows
   **Retention: 30 days**.
4. **Verify the failure queue stays empty** (SQS → `sfps-failed-events` → Send and receive
   messages → Poll): permanent-only events do not trigger SQS delivery. A mixed batch can
   still arrive if another record exhausts retries.
5. Optional failure-path check: temporarily set `TABLE_NAME` to a nonexistent table, upload
   a supported file, and confirm one or more matching messages appear in
   `sfps-failed-events` after the retries are exhausted. Inspect `requestContext` and
   `requestPayload`, then restore `TABLE_NAME`, delete the test message, and re-upload.

For a full acceptance close-out, verify one or more matching SQS invocation envelopes after
an injected infrastructure failure, restore `TABLE_NAME`, and measure a many-word 25 MiB
text file and images at the pixel boundary on the 512 MB / 30-second arm64 function. The
table-scoped IAM policy normally makes the missing-table test fail with access denial; the
public exception remains sanitized. Local tests and package inspection do not prove
deployed memory or timing acceptance.

## How it works

- **Processing identity.** Each S3 object version gets one deterministic record keyed by
  `processing_id` = SHA-256 of the canonical JSON encoding
  `{"bucket":...,"object_key":...,"version_id":...}` (sorted keys, compact separators),
  a fixed 64-char hex string.
- **Write protocol (conditional writes).** Claim: `PutItem` conditioned on
  `attribute_not_exists(processing_id)`. On a failed condition the existing record is read
  with `ConsistentRead=True`. Terminal: `UpdateItem` conditioned on
  `attribute_exists(processing_id) AND status <> COMPLETED`, so a `COMPLETED` record can
  never be overwritten. A terminal `ConditionalCheckFailedException` is treated as already
  satisfied.
- **Idempotency.** S3 event notifications are at-least-once. Duplicate delivery of the same
  version skips **only** `COMPLETED` records (logged explicitly, no changes); `PROCESSING`
  or `FAILED` records are re-attempted, so retries converge to the true terminal state.
  `created_at` is never overwritten.
- **Failure model.**
  - *Permanent validation failure* (bad extension/content, zero-byte, too large, wrong
    prefix): record `FAILED` with an error entry, log, return success, continue the batch —
    does not itself trigger retries or SQS delivery; another failing record can send the
    whole mixed batch to SQS.
  - *Transient/infrastructure failure* (AWS service error, unexpected exception,
    configuration error): logged with sanitized stack frames (CloudWatch only; exception
    text, source lines and locals omitted); a guarded `FAILED` update is attempted when the
    record exists; the exception is re-raised, so Lambda retries asynchronously
    (`MaximumRetryAttempts = 2`) and, when exhausted, the original event lands in
    `sfps-failed-events`.
  - *Malformed event* (missing `Records`, bucket, key, or `version_id`): raises
    immediately; no DynamoDB record is written.
- **Record schema.** `processing_id`, `bucket`, `object_key`, `version_id`, `file_name`,
  `file_type` (lowercased extension, omitted when none), `file_size` (N), `sha256`
  (COMPLETED only), `status` (`PROCESSING` | `COMPLETED` | `FAILED`), `created_at`,
  `processed_at` (UTC ISO-8601 ending in `Z`), `metadata` (M, COMPLETED only:
  width/height/`image_format` for images — `JPEG` or `PNG`; characters/words/lines for
  text), `error` (M, FAILED only: exactly `error_code`, `error_type`, `message`;
  serialized error stays within 1024 characters, truncating only `message`).

### Error codes

| `error_code` | `error_type` | Trigger |
|---|---|---|
| `ZERO_BYTE` | `ValidationError` | `HeadObject` reports 0 bytes |
| `SIZE_EXCEEDED` | `ValidationError` | Size greater than `MAX_FILE_SIZE_BYTES` |
| `UNSUPPORTED_EXTENSION` | `ValidationError` | Extension not in `{jpg, jpeg, png, txt}` |
| `TYPE_MISMATCH` | `ValidationError` | Content signature contradicts the extension |
| `CORRUPT_IMAGE` | `ValidationError` | Image integrity verification or decoding fails |
| `IMAGE_DIMENSIONS_EXCEEDED` | `ValidationError` | Total frame pixels exceed 16,000,000 or Pillow decompression-bomb protection fires |
| `INVALID_ENCODING` | `ValidationError` | `.txt` content is not valid strict UTF-8 |
| `PREFIX_NOT_ALLOWED` | `ValidationError` | Object key outside `incoming/` |
| `MALFORMED_EVENT` | `MalformedEventError` | Event or record cannot be safely interpreted |
| `INFRASTRUCTURE` | `InfrastructureError` | AWS service error or unexpected exception |

---

## Testing

`pytest` + `moto` + `ruff`. All tests run offline with no AWS credentials or network —
AWS calls are mocked with `moto` and the handler is invoked with synthetic S3 events.

```powershell
python -m pytest
python -m ruff check src tests
```

Main test files: `tests/test_validation.py`, `tests/test_checksum.py`,
`tests/test_image.py`, `tests/test_text.py`, `tests/test_handler.py`.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Uploads don't trigger Lambda | Notification still propagating (~5 min) or prefix mismatch | Wait and re-upload; confirm the trigger prefix is exactly `incoming/` (Lambda → Configuration → Triggers) |
| "Unable to validate the following destination configurations" | Function invoke permission missing/scoped wrong | Create the trigger from the Lambda console (step 13) or fix the resource policy (step 12) |
| AccessDenied reading S3 / writing DynamoDB / sending to SQS | IAM policy placeholder ARNs not replaced | Re-check each resource ARN in the role policy (step 8) |
| `Unable to import module 'handler'` | Zip layout wrong (folder inside zip, backslash entries, or handler name) | Rebuild the package (Deployment package section) and re-upload; verify handler `handler.lambda_handler` |
| No logs in CloudWatch | Log group missing (the role cannot create it by design) | Pre-create `/aws/lambda/sfps-processor` and set 30-day retention (step 14) |
| Record `FAILED` with `SIZE_EXCEEDED` | Object larger than 25 MiB default limit | Expected behavior; shrink the file. The configured limit cannot exceed 25 MiB |
| Folder upload records `FAILED` with `ZERO_BYTE` | Console folder creation writes zero-byte marker objects | Expected behavior |
| Message in `sfps-failed-events` | A transient failure exhausted retries | Inspect `requestContext`, `requestPayload` and sanitized CloudWatch frames; fix the cause, replay `requestPayload` asynchronously or re-upload (a new version) |
| Same object processed again after re-upload | A new upload creates a new object version (a new processing identity) | Expected — one record per version; duplicate deliveries of the *same* version are skipped |
