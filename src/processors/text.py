"""Pure text metadata extraction (PRD 7). No AWS calls."""

import re

_WORDS = re.compile(r"\S+")
_LINE_BREAKS = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")
_LINE_ENDINGS = ("\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")


def extract_metadata(text: str) -> dict:
    """Return ``{"characters", "words", "lines"}`` for decoded text.

    - ``characters``: Unicode code points (length of the decoded string)
    - ``words``: whitespace-separated runs (``str.split`` semantics)
    - ``lines``: ``str.splitlines`` result; a trailing newline adds no line

    The input is the decoded, BOM-stripped text returned by
    ``validation.decode_text``; this module never re-decodes bytes.
    """
    return {
        "characters": len(text),
        "words": sum(1 for _ in _WORDS.finditer(text)),
        "lines": sum(1 for _ in _LINE_BREAKS.finditer(text))
        + int(bool(text) and not text.endswith(_LINE_ENDINGS)),
    }
