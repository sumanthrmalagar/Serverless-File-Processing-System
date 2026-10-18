"""SHA-256 checksum of raw file bytes (PRD 8).

No AWS calls and no file I/O: the handler passes the already-fetched,
version-pinned object bytes.
"""

import hashlib


def sha256_hex(data: bytes) -> str:
    """Return the lowercase hexadecimal SHA-256 digest of *data*."""
    return hashlib.sha256(data).hexdigest()
