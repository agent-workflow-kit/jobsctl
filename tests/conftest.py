"""Shared fixtures: a private HOME/XDG guard and JSON-schema helpers."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"


@pytest.fixture(autouse=True)
def private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point HOME and every XDG directory at a private temp dir for every test.

    Nothing a test starts may read or write the real home, config or state directories.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name, sub in (
        ("XDG_CONFIG_HOME", ".config"),
        ("XDG_STATE_HOME", ".local/state"),
        ("XDG_DATA_HOME", ".local/share"),
        ("XDG_CACHE_HOME", ".cache"),
    ):
        path = home / sub
        path.mkdir(parents=True)
        monkeypatch.setenv(name, str(path))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    yield home


def _registry() -> Registry[Any]:
    registry: Registry[Any] = Registry()
    for path in sorted(SCHEMAS.rglob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        registry = registry.with_resource(doc["$id"], Resource.from_contents(doc))
    return registry


def validate_against(schema_name: str, instance: object) -> None:
    """Validate ``instance`` against ``schemas/<schema_name>`` (e.g. ``cli/error.json``)."""
    doc = json.loads((SCHEMAS / schema_name).read_text(encoding="utf-8"))
    Draft202012Validator(doc, registry=_registry()).validate(instance)


def validate_record_file(path: Path, schema_name: str, *, torn: int = 0) -> list[dict[str, Any]]:
    """Validate every line of the record file ``path`` against ``schemas/<schema_name>``.

    Records written by child processes bypass ``validated_records``, so process tests check the
    files afterwards. Exactly ``torn`` lines may be torn fragments (not JSON); every other line
    must be a record matching the schema. Returns the records in file order.
    """
    records: list[dict[str, Any]] = []
    fragments = 0
    for line in path.read_text(encoding="ascii").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            fragments += 1
            continue
        validate_against(schema_name, record)
        records.append(record)
    assert fragments == torn, f"{path.name}: {fragments} torn lines, expected {torn}"
    return records


@pytest.fixture
def validated_records(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validate every record a ``RecordWriter`` appends in this process against its schema.

    Audit appends are the durable ones, so ``durable`` tells the two files apart. A record that
    does not match fails the test (``ValidationError`` is not a ``ValueError``). Tests that write
    foreign or torn lines straight to a file bypass the writer and are not checked.
    """
    from jobsctl import log_writer

    real = log_writer.append_line

    def checked(fd: int, data: bytes, *, durable: bool) -> None:
        validate_against("audit.json" if durable else "log.json", json.loads(data))
        real(fd, data, durable=durable)

    monkeypatch.setattr(log_writer, "append_line", checked)
