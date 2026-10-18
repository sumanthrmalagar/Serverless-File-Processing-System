"""Unit tests for the SHA-256 helper (no AWS, no network)."""

from checksum import sha256_hex

EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_sha256_hex_empty_bytes() -> None:
    assert sha256_hex(b"") == EMPTY_SHA256


def test_sha256_hex_known_digest_abc() -> None:
    assert sha256_hex(b"abc") == ABC_SHA256


def test_sha256_hex_lowercase_hex_for_larger_fixture() -> None:
    digest = sha256_hex(bytes(range(256)) * 8)
    assert len(digest) == 64
    assert all(character in "0123456789abcdef" for character in digest)
