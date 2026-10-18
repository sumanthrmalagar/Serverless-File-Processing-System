"""Test-session environment defaults and shared helpers.

``handler.py`` validates configuration at import time (PRD §5: ``TABLE_NAME`` is
required) and creates boto3 clients at import time, so these variables must
exist before any test module imports the handler. The values are harmless
dummies; no real AWS access ever happens because AWS calls are mocked with
moto inside the tests.
"""

import io
import os

from PIL import Image

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("TABLE_NAME", "sfps-processing")


def make_image_bytes(image_format: str, size: tuple[int, int] = (2, 3)) -> bytes:
    """Build a tiny valid in-memory image fixture (single color, 2x3 px default)."""
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 100, 50)).save(buffer, format=image_format)
    return buffer.getvalue()
