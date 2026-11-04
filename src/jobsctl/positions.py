"""Position keys for new jobs (interface.md, Order within a queue).

A position is sortable text. This module only assigns the keys `add` needs: the end of the
queue (FIFO, which is also age order) and the top of a priority band. Keys are 20-digit
zero-padded integers around a midpoint, so text order is numeric order and `move` can later
insert between two keys with a fractional suffix (`...5000.5` sorts between `...5000` and
`...5001`). TODO(#10): `order.py` owns position keys; it takes over these two helpers.
"""

from __future__ import annotations

from typing import Final

from jobsctl.store_db import Tx

_WIDTH: Final = 20
MIDPOINT: Final = 10**15
_LIVE: Final = ("queued", "held")  # the statuses that keep a place in the queue


def _key(number: int) -> str:
    if not 0 <= number < 10**_WIDTH:
        raise OverflowError("position key out of range")
    return f"{number:0{_WIDTH}d}"


def _number(position: str) -> int:
    """The integer part of a stored key (a fractional suffix from `move` is dropped)."""
    return int(position.partition(".")[0])


def tail_position(tx: Tx, queue: str) -> str:
    """A key after every queued or held job of `queue`."""
    row = tx.one(
        "SELECT max(position) AS p FROM jobs WHERE queue = ? AND status IN (?, ?)",
        [queue, *_LIVE],
    )
    if row is None or row["p"] is None:
        return _key(MIDPOINT)
    return _key(_number(str(row["p"])) + 1)


def head_position(tx: Tx, queue: str, *, pinned: bool, priority: int) -> str:
    """A key before every queued or held job of the same pinned group and priority band; the
    tail key when the band is empty (its age position)."""
    row = tx.one(
        "SELECT min(position) AS p FROM jobs "
        "WHERE queue = ? AND status IN (?, ?) AND pinned = ? AND priority = ?",
        [queue, *_LIVE, int(pinned), priority],
    )
    if row is None or row["p"] is None:
        return tail_position(tx, queue)
    return _key(_number(str(row["p"])) - 1)
