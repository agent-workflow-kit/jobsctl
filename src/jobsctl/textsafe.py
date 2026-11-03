"""Sanitizer for human output (interface.md, Masking and sanitizing, steps 3 and 5).

Masking (step 4) needs the masked values and belongs to the output-capture issue.
"""

from __future__ import annotations

import re
import unicodedata

# 7-bit and 8-bit forms of CSI, OSC, DCS, SOS, PM and APC. Unterminated string sequences end at
# the end of the text, so a stray introducer cannot hide what follows it from a reader.
_ESCAPES = re.compile(
    r"""
      (?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]
    | (?:\x1b[\]PX^_]|[\x9d\x90\x98\x9e\x9f])(?:.*?)(?:\x07|\x1b\\|\x9c|\Z)
    | \x1b[@-Z\\-_]
    """,
    re.VERBOSE | re.DOTALL,
)
_BIDI_AND_INVISIBLE = {
    0x061C,
    0x200E,
    0x200F,
    *range(0x202A, 0x202F),
    *range(0x2066, 0x206A),
}


def _escape_char(ch: str) -> str:
    code = ord(ch)
    if 0xDC80 <= code <= 0xDCFF:  # an undecodable byte kept by surrogateescape
        return f"\\x{code - 0xDC00:02x}"
    if ch in "\t\n":
        return ch
    category = unicodedata.category(ch)
    if category in ("Cc", "Cs", "Cf") or code in _BIDI_AND_INVISIBLE:
        return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"
    return ch


def sanitize(text: str) -> str:
    """Strip terminal escape sequences, keep the last segment of `\\r`-rewritten lines and escape
    control, bidirectional and invisible format characters as `\\uNNNN`."""
    text = _ESCAPES.sub("", text)
    lines = [line.rsplit("\r", 1)[-1] for line in text.replace("\r\n", "\n").split("\n")]
    return "".join(_escape_char(ch) for ch in "\n".join(lines))


def echo(value: str, limit: int = 80) -> str:
    """An input quoted back in a message: sanitized and cut to `limit` characters."""
    clean = sanitize(value)
    return clean if len(clean) <= limit else clean[:limit] + "..."
