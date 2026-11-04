"""Sanitizer for human output (interface.md, Masking and sanitizing, steps 3 and 5).

Masking (step 4) needs the masked values: `env_mask.py` holds the set and `output_tail.py` the
pipeline that masks between `strip_escapes` and `escape_unsafe`.
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
_STRING_8BIT = ("\x9d", "\x90", "\x98", "\x9e", "\x9f")
_STRING_7BIT = ("\x1b]", "\x1bP", "\x1bX", "\x1b^", "\x1b_")
_TERMINATORS = ("\x07", "\x9c", "\x1b\\")
# What can still become an escape sequence at the end of a text: a lone ESC, or a CSI with no
# final byte yet.
_DANGLING = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*\Z|\x1b\Z")
_BIDI_AND_INVISIBLE = {
    0x061C,
    0x200E,
    0x200F,
    *range(0x202A, 0x202F),
    *range(0x2066, 0x206A),
}


def escape_surrogates(text: str) -> str:
    """Replace lone surrogates with text: `\\xNN` for an undecodable byte kept by
    surrogateescape, `\\uNNNN` for any other, so the result encodes to UTF-8."""
    if not any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        return text
    return "".join(_escape_surrogate(ch) for ch in text)


def _escape_surrogate(ch: str) -> str:
    code = ord(ch)
    if 0xDC80 <= code <= 0xDCFF:
        return f"\\x{code - 0xDC00:02x}"
    if 0xD800 <= code <= 0xDFFF:
        return f"\\u{code:04x}"
    return ch


def _escape_char(ch: str) -> str:
    code = ord(ch)
    if 0xD800 <= code <= 0xDFFF:
        return _escape_surrogate(ch)
    if ch in "\t\n":
        return ch
    category = unicodedata.category(ch)
    if category in ("Cc", "Cs", "Cf", "Zl", "Zp") or code in _BIDI_AND_INVISIBLE:
        return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"
    return ch


def sanitize(text: str, *, inline: bool = False) -> str:
    """Strip terminal escape sequences and escape control, bidirectional, invisible format and
    line or paragraph separator characters as `\\uNNNN`.

    By default (captured output) keep the last segment of `\\r`-rewritten lines. With `inline`
    (error messages and echoed input) a `\\r` or `\\n` becomes the two characters `\\r` or `\\n`,
    so the text cannot rewrite, erase or forge a line.
    """
    text = _ESCAPES.sub("", text)
    if inline:
        text = text.replace("\r", "\\r").replace("\n", "\\n")
        return "".join(_escape_char(ch) for ch in text)
    lines = [line.rsplit("\r", 1)[-1] for line in text.replace("\r\n", "\n").split("\n")]
    return "".join(_escape_char(ch) for ch in "\n".join(lines))


def echo(value: str, limit: int = 80) -> str:
    """An input quoted back in a message: sanitized inline and cut to `limit` characters."""
    clean = sanitize(value, inline=True)
    return clean if len(clean) <= limit else clean[:limit] + "..."


def strip_escapes(text: str) -> str:
    """Masking and sanitizing step 3 alone: remove terminal escape sequences and keep the last
    segment of `\\r`-rewritten lines. Captured output is masked between this and `escape_unsafe`
    (step 5), so the two steps are separate."""
    text = _ESCAPES.sub("", text)
    lines = [line.rsplit("\r", 1)[-1] for line in text.replace("\r\n", "\n").split("\n")]
    return "\n".join(lines)


def escape_unsafe(text: str) -> str:
    """Masking and sanitizing step 5 alone: escape control, bidirectional, invisible format and
    separator characters as `\\uNNNN`, keeping tab and newline."""
    return "".join(_escape_char(ch) for ch in text)


def incomplete_escape_start(text: str) -> int:
    """Where an escape sequence that more output could still complete or change begins at the
    end of `text` (`len(text)` if there is none): an unterminated string sequence (which
    `strip_escapes` removes to the end of the text, and which a later terminator would end
    sooner), a CSI missing its final byte, or a lone ESC. A live tail withholds from there so a
    later byte never changes text that was already shown."""
    last_end = 0
    for m in _ESCAPES.finditer(text):
        last_end = m.end()
        body = m.group()
        is_string = body[0] in _STRING_8BIT or body[:2] in _STRING_7BIT
        if is_string and not body.endswith(_TERMINATORS):
            return m.start()
    dangling = _DANGLING.search(text, last_end)
    return dangling.start() if dangling else len(text)


def strip_escapes_mapped(text: str) -> tuple[str, list[int]]:
    """`strip_escapes(text)` and, for each character of the result, its index in `text` (the
    indexes increase), so what was found in `text` can be located in the stripped text."""
    kept: list[tuple[int, int]] = []
    pos = 0
    for m in _ESCAPES.finditer(text):
        if m.start() > pos:
            kept.append((pos, m.start()))
        pos = m.end()
    if pos < len(text):
        kept.append((pos, len(text)))
    stripped = "".join(text[a:b] for a, b in kept)
    index = [i for a, b in kept for i in range(a, b)]
    # The `\r` rewrites, as the characters to drop: a `\r` before a newline is part of `\r\n`,
    # and each line keeps only what follows its last `\r`.
    drops: list[tuple[int, int]] = []
    offset = 0
    lines = stripped.split("\n")
    for n, line in enumerate(lines):
        end = len(line) - 1 if n < len(lines) - 1 and line.endswith("\r") else len(line)
        cr = line.rfind("\r", 0, end)
        if cr >= 0:
            drops.append((offset, offset + cr + 1))
        if end < len(line):
            drops.append((offset + end, offset + end + 1))
        offset += len(line) + 1
    chars: list[str] = []
    origin: list[int] = []
    pos = 0
    for start, stop in drops:
        chars.append(stripped[pos:start])
        origin += index[pos:start]
        pos = stop
    chars.append(stripped[pos:])
    origin += index[pos:]
    return "".join(chars), origin
