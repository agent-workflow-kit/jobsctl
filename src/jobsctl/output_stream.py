"""Masking and sanitizing a whole stream of output as it arrives (interface.md, Full output log).

`StreamMasker` takes raw chunks and returns, for each, the text that later output cannot change,
already masked and sanitized by the same pipeline as the tail (`output_tail`: steps 2 to 5; no
step 1 and no cut, as nothing is dropped). It keeps the *pending* text: what a later chunk could
still change or complete. Per `output_tail.render_stable` that is everything after the last
newline, at least the longest masked value minus one character at the end, an unfinished
escape sequence or character, and anything that a masked value (found in the raw or the stripped
text) straddles, so a secret split over two chunks, at any offset, is never written in part.

Memory is bounded: pending text over `max_pending` characters is flushed by
`output_tail.render_forced`, which keeps the last (longest masked value) characters; when even
that finds no safe cut, `StreamOverflow` is raised and the caller stops writing (nothing unsafe
is ever returned). While a render keeps a long text
(`BACKOFF_MIN` characters or more) entirely pending, the next one waits until it has grown by
an eighth, so a long unfinished escape sequence costs a geometric, not a quadratic, number of
renders; `finish` always renders.

Chunks are decoded incrementally (UTF-8, invalid bytes as `\\xNN`), which equals decoding the
whole stream, so a multi-byte character split over two chunks is intact.
"""

from __future__ import annotations

import codecs
from typing import Final

from jobsctl.env_mask import MaskingSet
from jobsctl.output_tail import render_final, render_forced, render_stable

MAX_PENDING: Final = 1 << 20  # characters held beyond the longest masked value
BACKOFF_MIN: Final = 4096  # pending characters from which a stalled render backs off


class StreamOverflow(Exception):
    """The pending text is too long and has no safe cut."""


class StreamMasker:
    def __init__(self, masking: MaskingSet, *, max_pending: int = MAX_PENDING) -> None:
        self._masking = masking
        # Room for the longest masked value twice over, so a forced flush always has a cut.
        self._limit = max_pending + 2 * masking.longest_bytes
        self._decoder = codecs.getincrementaldecoder("utf-8")("backslashreplace")
        self._pending = ""
        # The pending length at the last render that consumed nothing (an escape string held
        # open, say); None when the last render made progress.
        self._stalled_at: int | None = None

    def feed(self, data: bytes) -> str:
        """The text that is now safe to write ('' when nothing is)."""
        text = self._decoder.decode(data, False)
        self._pending += text
        # Output that cannot have moved the stable end (no newline in it) waits for the next one.
        if "\n" not in text and len(self._pending) <= self._limit:
            return ""
        # When everything was held last time, rendering again at every read would be quadratic
        # (a string escape that stays open): wait until the pending text has grown by 1/8.
        stalled = self._stalled_at
        if stalled is not None and len(self._pending) < stalled + stalled // 8:
            return ""
        rendered = render_stable(self._pending, self._masking)
        out = rendered.text
        consumed = rendered.consumed
        if len(self._pending) - consumed > self._limit:
            forced = render_forced(self._pending[consumed:], self._masking)
            if forced.consumed == 0:
                raise StreamOverflow
            out += forced.text
            consumed += forced.consumed
        # (short pending text is cheap to render again, so only a long one backs off)
        held = consumed == 0 and len(self._pending) >= BACKOFF_MIN
        self._stalled_at = len(self._pending) if held else None
        self._pending = self._pending[consumed:]
        return out

    def finish(self) -> str:
        """The rest, at the end of the stream: a trailing partial line, an incomplete character
        (as `\\xNN`) and an unfinished escape sequence (removed) are all rendered."""
        self._pending += self._decoder.decode(b"", True)
        out = render_final(self._pending, self._masking)
        self._pending = ""
        return out
