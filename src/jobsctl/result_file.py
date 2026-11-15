"""The result file `shims/<id>.<attempt>.result` (docs/interface.md, Start sequence step 9).

The shim writes the outcome of the job here before its final commit, so a shim that dies between
the two leaves what reconcile needs to finalize the attempt (`result_file_finalized`,
`jobsctl.reconcile`).
The name is predictable and lives in a directory only the user can write, but the file is still
created defensively: the directory is opened without following symlinks and every operation is
relative to that descriptor; the content goes to an exclusively created temporary file (0600,
`O_NOFOLLOW`) that is flushed and then renamed over the final name, so a reader sees all of it or
nothing, and a pre-planted symlink or file at the final name is replaced, never followed or
written through. Reading refuses anything but a small regular file owned by the user with mode
0600.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from jobsctl.shim_exit import ExitRecord

VERSION: Final = 1
MAX_BYTES: Final = 2 * 1024 * 1024  # a tail is at most 256 KiB; escapes may widen it


class ResultFileError(Exception):
    """The file cannot be written, or what is there cannot be trusted."""


@dataclass(frozen=True)
class ResultTail:
    text: str
    bytes_seen: int
    truncated: bool


@dataclass(frozen=True)
class ResultRecord:
    job_id: str
    attempt: int
    claim_id: str
    status: str  # done, failed, held (what the exit alone decides; a force request is re-read)
    reason: str | None
    hold_reason: str | None
    exit: ExitRecord
    leftovers: int | None
    tail: ResultTail
    ended_at: str

    def as_json(self) -> dict[str, Any]:
        return {
            "version": VERSION,
            "job_id": self.job_id,
            "attempt": self.attempt,
            "claim_id": self.claim_id,
            "status": self.status,
            "reason": self.reason,
            "hold_reason": self.hold_reason,
            "exit": self.exit.as_json(),
            "leftovers": self.leftovers,
            "tail": {
                "text": self.tail.text,
                "bytes_seen": self.tail.bytes_seen,
                "truncated": self.tail.truncated,
            },
            "ended_at": self.ended_at,
        }


def result_name(job_id: str, attempt: int) -> str:
    return f"{job_id}.{attempt}.result"


def _open_dir(shims: Path) -> int:
    try:
        return os.open(shims, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise ResultFileError(f"cannot open the shims directory ({exc.strerror})") from exc


def write_result(shims: Path, record: ResultRecord) -> str:
    """Write `record` atomically; returns the file name. Raises `ResultFileError`."""
    data = json.dumps(record.as_json(), sort_keys=True, ensure_ascii=True).encode("ascii")
    final = result_name(record.job_id, record.attempt)
    temp = f"{final}.tmp-{secrets.token_hex(8)}"
    dir_fd = _open_dir(shims)
    created = False
    try:
        fd = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=dir_fd,
        )
        created = True
        try:
            os.fchmod(fd, 0o600)  # a restrictive umask may have removed owner bits
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(temp, final, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        created = False
        os.fsync(dir_fd)
    except OSError as exc:
        raise ResultFileError(f"cannot write {final} ({exc.strerror})") from exc
    finally:
        if created:
            try:
                os.unlink(temp, dir_fd=dir_fd)
            except OSError:
                pass
        os.close(dir_fd)
    return final


def read_result(shims: Path, job_id: str, attempt: int) -> ResultRecord | None:
    """The result file of an attempt; `None` when there is none. Raises `ResultFileError` for a
    file that cannot be trusted (a symlink, a FIFO, another owner, loose mode, too big, damaged)."""
    name = result_name(job_id, attempt)
    dir_fd = _open_dir(shims)
    try:
        try:
            fd = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=dir_fd,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ResultFileError(f"cannot open {name} ({exc.strerror})") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise ResultFileError(f"{name} is not a regular file")
            if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o600:
                raise ResultFileError(f"{name} has the wrong owner or mode")
            if st.st_size > MAX_BYTES:
                raise ResultFileError(f"{name} is too large")
            raw = os.read(fd, MAX_BYTES + 1)
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)
    return _parse(raw, job_id, attempt)


def _parse(raw: bytes, job_id: str, attempt: int) -> ResultRecord:
    try:
        doc = json.loads(raw)
        if doc["version"] != VERSION or doc["job_id"] != job_id or doc["attempt"] != attempt:
            raise ValueError("wrong version, job or attempt")
        tail = doc["tail"]
        record = ResultRecord(
            job_id=doc["job_id"],
            attempt=doc["attempt"],
            claim_id=_text(doc["claim_id"]),
            status=_text(doc["status"]),
            reason=_text_or_none(doc["reason"]),
            hold_reason=_text_or_none(doc["hold_reason"]),
            exit=ExitRecord.from_json(doc["exit"]),
            leftovers=_int_or_none(doc["leftovers"]),
            tail=ResultTail(_text(tail["text"]), _int(tail["bytes_seen"]), bool(tail["truncated"])),
            ended_at=_text(doc["ended_at"]),
        )
    except Exception as exc:  # also RecursionError of a deeply nested document
        raise ResultFileError(f"{result_name(job_id, attempt)} is damaged") from exc
    if record.status not in ("done", "failed", "held"):
        raise ResultFileError(f"{result_name(job_id, attempt)} records an unknown status")
    return record


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("not text")
    return value


def _text_or_none(value: object) -> str | None:
    return None if value is None else _text(value)


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("not an integer")
    return value


def _int_or_none(value: object) -> int | None:
    return None if value is None else _int(value)


def delete_result(shims: Path, job_id: str, attempt: int) -> None:
    """Remove the file (a no-op when absent). Never follows a symlink: it removes the name."""
    dir_fd = _open_dir(shims)
    try:
        os.unlink(result_name(job_id, attempt), dir_fd=dir_fd)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ResultFileError(f"cannot remove the result file ({exc.strerror})") from exc
    finally:
        os.close(dir_fd)
