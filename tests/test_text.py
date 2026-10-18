"""Unit tests for the text metadata processor (no AWS, no network)."""

from processors.text import extract_metadata


def test_hello_world_counts() -> None:
    assert extract_metadata("hello world") == {"characters": 11, "words": 2, "lines": 1}


def test_trailing_newline_adds_no_line() -> None:
    assert extract_metadata("a b\nc d\n") == {"characters": 8, "words": 4, "lines": 2}


def test_whitespace_only_has_zero_words() -> None:
    assert extract_metadata("   \n\t") == {"characters": 5, "words": 0, "lines": 2}


def test_empty_string_is_all_zeros() -> None:
    assert extract_metadata("") == {"characters": 0, "words": 0, "lines": 0}


def test_unicode_counts_code_points_not_bytes() -> None:
    assert extract_metadata("héllo")["characters"] == 5


def test_crlf_line_endings() -> None:
    assert extract_metadata("line1\r\nline2")["lines"] == 2


def test_returns_exact_key_set() -> None:
    assert set(extract_metadata("hello")) == {"characters", "words", "lines"}
