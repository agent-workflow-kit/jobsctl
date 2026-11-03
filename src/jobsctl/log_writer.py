"""Writers for ``audit.jsonl`` and ``log.jsonl`` (interface.md, Audit and log).

``seq`` comes from a per-file counter in ``state.db`` (``seq:audit``, ``seq:log``). A writer
takes the file's exclusive ``flock``, increments the counter in its own short write transaction,
appends one line and releases the lock, so records are in ``seq`` order with no interleaving
and ``seq`` has gaps only when a process dies between the increment and the append (a failed
append that this process survives gives the number back). The counter lives in the database, so
it survives an external rotation of the file.

No record is appended inside a database write transaction: call a writer after the commit,
from :meth:`~jobsctl.store_db.Tx.after_commit`. A writer used inside a transaction raises
:class:`RuntimeError` before touching any file.

Never written: ``--pass-env`` or ``env_file`` values, argv, raw hook output, output tails or
``--env`` values. The record builders accept only schema fields, refuse ``details`` keys that
would carry those, and replace every value of the masking set in any string with ``***``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from jobsctl import gateway
from jobsctl.errors import JobsctlError
from jobsctl.log_files import append_line, locked
from jobsctl.log_records import (
    build_audit_record,
    build_log_record,
    encode_line,
    format_ts,
    masking_set,
)
from jobsctl.store_db import Store
from jobsctl.store_ops import counter_cas, counter_get, counter_incr

AUDIT_FILE = "audit.jsonl"
LOG_FILE = "log.jsonl"
AUDIT_COUNTER = "seq:audit"
LOG_COUNTER = "seq:log"
_WARN_PREFIX = "warn:"


def warn_counter_name(key: str) -> str:
    """The counter that remembers the last state logged for ``key`` (a bounded, safe name)."""
    return _WARN_PREFIX + hashlib.sha256(key.encode("utf-8", "surrogateescape")).hexdigest()[:32]


def _state_fingerprint(state: str) -> int:
    """A positive 63-bit number for ``state``; 0 is reserved for "no warning outstanding"."""
    digest = hashlib.sha256(state.encode("utf-8", "surrogateescape")).digest()
    return (int.from_bytes(digest[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF) or 1


class RecordWriter:
    """Appends records to ``audit.jsonl`` and ``log.jsonl`` of one store's state directory.

    ``masked_values`` is the masking set (values of ``mask_env``, ``--pass-env`` and
    ``env_file`` variables); ``clock`` returns the current UTC time.
    """

    def __init__(
        self,
        store: Store,
        *,
        masked_values: Iterable[str] = (),
        clock: Callable[[], dt.datetime] = gateway.utc_now,
    ) -> None:
        # TODO(#8): the masking set comes from the env module (mask_env, pass-env, env_file).
        self._store = store
        self._masked = masking_set(masked_values)
        self._clock = clock

    # -- public API ------------------------------------------------------------------------

    def log(
        self,
        event: str,
        *,
        level: str = "info",
        fields: Mapping[str, Any] | None = None,
    ) -> int:
        """Append one ``log.jsonl`` record and return its ``seq``."""
        body = build_log_record(event, level, fields, self._masked)
        return self._append(LOG_FILE, LOG_COUNTER, body, durable=False)

    def audit(
        self,
        *,
        action: str,
        actor: str,
        target: str | None,
        inputs: Mapping[str, Any],
        result: str,
        decision_id: Sequence[str] = (),
    ) -> int:
        """Append one ``audit.jsonl`` record and return its ``seq``.

        Audit records are synced to disk (they are kept forever; interface.md leaves the
        policy open, and they are rare).
        """
        body = build_audit_record(
            action=action,
            actor=actor,
            decision_id=decision_id,
            target=target,
            inputs=inputs,
            result=result,
            masked=self._masked,
        )
        return self._append(AUDIT_FILE, AUDIT_COUNTER, body, durable=True)

    def warn_once(
        self,
        key: str,
        state: str | None,
        event: str,
        *,
        level: str = "warn",
        fields: Mapping[str, Any] | None = None,
    ) -> bool:
        """Log a state warning once per change of state; return whether a record was written.

        ``key`` names the subject (for example the event and the path); ``state`` is a
        fingerprint of the current condition (for example the error and the file's identity),
        or ``None`` when the condition is gone, which re-arms the warning. The last state is
        kept in ``state.db``, so repeated passes in different processes log it once; of
        several racing processes exactly one logs. Call it outside a transaction.

        The state is recorded before the record is appended: a crash in between loses the
        warning rather than repeating it.
        """
        self._require_no_transaction()
        name = warn_counter_name(key)
        wanted = 0 if state is None else _state_fingerprint(state)
        with self._store.write() as tx:
            current = counter_get(tx, name)
            if current == wanted:
                return False
            if not counter_cas(tx, name, current, wanted):  # cannot lose under BEGIN IMMEDIATE
                raise JobsctlError("internal", "warning state changed under the write lock")
        if state is None:
            return False
        self.log(event, level=level, fields=fields)
        return True

    # -- internals -------------------------------------------------------------------------

    def _require_no_transaction(self) -> None:
        if self._store.conn.in_transaction:
            raise RuntimeError(
                "records are never written inside a database transaction; "
                "use Tx.after_commit to log after the commit"
            )

    def _append(self, filename: str, counter: str, body: dict[str, Any], *, durable: bool) -> int:
        self._require_no_transaction()
        encode_line({"seq": 0, "ts": "", **body}, shrink_details=filename == LOG_FILE)
        path = self._store.paths.root / filename
        with locked(path) as fd:
            with self._store.write() as tx:  # its own short transaction, under the file lock
                seq = counter_incr(tx, counter)
            try:
                record = {"seq": seq, "ts": format_ts(self._clock()), **body}
                line = encode_line(record, shrink_details=filename == LOG_FILE)
                append_line(fd, line, durable=durable)
            except BaseException:
                self._give_back(counter, seq)
                raise
        return seq

    def _give_back(self, counter: str, seq: int) -> None:
        """Undo the increment after a failed append (no other writer can have taken a number:
        they all wait for the file lock this process holds)."""
        try:
            with self._store.write() as tx:
                counter_cas(tx, counter, seq, seq - 1)
        except BaseException:  # a gap is allowed; never mask the original error
            pass
