"""`move`, `pin`, `unpin`, `priority`, `capacity` and `queues`."""

from __future__ import annotations

import json
import os
import random
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from jobsctl.order import integer_key
from jobsctl.store_dir import resolve_state_dir
from tests.conftest import validate_against, validate_record_file
from tests.helpers import Result, run_cli

pytestmark = pytest.mark.usefixtures("validated_records")


@pytest.fixture(autouse=True)
def clean_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")


def write_config(tmp_path: Path, **keys: Any) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(keys), encoding="utf-8")  # JSON is YAML
    return str(path)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(resolve_state_dir() / "state.db")
    conn.row_factory = sqlite3.Row
    return conn


def sql(statement: str, *params: Any) -> None:
    conn = db()
    try:
        conn.execute(statement, params)
        conn.commit()
    finally:
        conn.close()


def add(capsys: pytest.CaptureFixture[str], *flags: str, queue: str = "q") -> str:
    res = run_cli(capsys, "--json", "add", "--queue", queue, *flags, "--cwd", "/w", "--", "true")
    assert res.code == 0, res.out + res.err
    return str(res.envelope()["job"]["id"])


def run(capsys: pytest.CaptureFixture[str], *argv: str, config: str | None = None) -> Result:
    pre = ["--config", config] if config else []
    return run_cli(capsys, "--json", *pre, *argv)


def change(res: Result) -> dict[str, Any]:
    assert res.code == 0, res.out + res.err
    doc = res.envelope()
    validate_against("cli/change.json", doc)
    return doc


def queued_order(queue: str = "q") -> list[str]:
    """Queued job ids by an independent sort (pinned, priority, text position, id)."""
    conn = db()
    try:
        rows = conn.execute(
            "SELECT id, pinned, priority, position FROM jobs WHERE queue = ? AND status = 'queued'",
            [queue],
        ).fetchall()
    finally:
        conn.close()
    rows.sort(key=lambda r: (-r["pinned"], -r["priority"], r["position"], r["id"]))
    return [r["id"] for r in rows]


def positions(queue: str = "q") -> list[str]:
    conn = db()
    try:
        return [
            r[0]
            for r in conn.execute(
                "SELECT position FROM jobs WHERE queue = ? AND status IN ('queued', 'held')",
                [queue],
            )
        ]
    finally:
        conn.close()


def ranks(capsys: pytest.CaptureFixture[str], ids: list[str]) -> list[int]:
    """The `rank` each job reports (through a no-op `priority` call)."""
    out = []
    for job_id in ids:
        row = db().execute("SELECT priority FROM jobs WHERE id = ?", [job_id]).fetchone()
        doc = change(run(capsys, "priority", job_id, str(row["priority"])))
        out.append(doc["job"]["rank"])
    return out


def jobs(capsys: pytest.CaptureFixture[str], n: int, **kw: Any) -> list[str]:
    return [add(capsys, **kw) for _ in range(n)]


# -- add and rank ----------------------------------------------------------------------------


def test_add_is_fifo_and_rank_is_one_based(capsys: pytest.CaptureFixture[str]) -> None:
    a, b, c = jobs(capsys, 3)
    assert queued_order() == [a, b, c]
    assert ranks(capsys, [a, b, c]) == [1, 2, 3]


def test_rank_is_per_queue_and_null_unless_queued(capsys: pytest.CaptureFixture[str]) -> None:
    a = add(capsys, queue="one")
    b = add(capsys, queue="two")
    assert ranks(capsys, [a, b]) == [1, 1]
    sql("UPDATE jobs SET status = 'held', hold_reason = 'manual' WHERE id = ?", a)
    doc = change(run(capsys, "priority", a, "0"))
    assert doc["job"]["rank"] is None
    assert doc["job"]["status"] == "held"


def test_add_top_lands_before_the_band(capsys: pytest.CaptureFixture[str]) -> None:
    a, b = jobs(capsys, 2)
    c = add(capsys, "--top")
    assert queued_order() == [c, a, b]
    d = add(capsys, "--top", "--priority", "5")
    assert queued_order() == [d, c, a, b]


def test_priority_and_pin_bands_order_rank(capsys: pytest.CaptureFixture[str]) -> None:
    a, b, c, d = jobs(capsys, 4)
    run(capsys, "priority", c, "3")
    run(capsys, "pin", d)
    run(capsys, "priority", b, "-1")
    assert queued_order() == [d, c, a, b]
    assert ranks(capsys, [d, c, a, b]) == [1, 2, 3, 4]


# -- move ------------------------------------------------------------------------------------


def test_move_before_and_after(capsys: pytest.CaptureFixture[str]) -> None:
    a, b, c, d = jobs(capsys, 4)
    doc = change(run(capsys, "move", d, "--before", b))
    assert doc["changed"] is True and doc["dry_run"] is False
    assert queued_order() == [a, d, b, c]
    change(run(capsys, "move", a, "--after", c))
    assert queued_order() == [d, b, c, a]
    assert ranks(capsys, [d, b, c, a]) == [1, 2, 3, 4]


def test_move_top_and_bottom(capsys: pytest.CaptureFixture[str]) -> None:
    a, b, c = jobs(capsys, 3)
    change(run(capsys, "move", c, "--top"))
    assert queued_order() == [c, a, b]
    change(run(capsys, "move", c, "--bottom"))
    assert queued_order() == [a, b, c]


def test_move_to_where_it_already_is_changes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    a, b, c = jobs(capsys, 3)
    before = positions()
    for argv in (
        ("move", a, "--top"),
        ("move", c, "--bottom"),
        ("move", b, "--after", a),
        ("move", b, "--before", c),
        ("move", b, "--before", b),
        ("move", b, "--after", b),
    ):
        doc = change(run(capsys, *argv))
        assert doc["changed"] is False, argv
    assert sorted(positions()) == sorted(before)
    assert queued_order() == [a, b, c]


def test_move_adopts_the_priority_and_pin_of_the_neighbour(
    capsys: pytest.CaptureFixture[str],
) -> None:
    a, b, c = jobs(capsys, 3)
    run(capsys, "priority", a, "7")
    run(capsys, "pin", a)
    doc = change(run(capsys, "move", c, "--after", a))
    assert doc["job"]["priority"] == 7 and doc["job"]["pinned"] is True
    assert queued_order() == [a, c, b]
    doc = change(run(capsys, "move", c, "--before", b))  # b is unpinned priority 0
    assert doc["job"]["priority"] == 0 and doc["job"]["pinned"] is False
    assert queued_order() == [a, c, b]


def test_top_and_bottom_stay_within_the_own_band(capsys: pytest.CaptureFixture[str]) -> None:
    a, b, c, d = jobs(capsys, 4)
    run(capsys, "priority", a, "5")
    run(capsys, "pin", b)
    # order: b (pinned), a (5), c, d
    assert queued_order() == [b, a, c, d]
    change(run(capsys, "move", d, "--top"))
    assert queued_order() == [b, a, d, c]  # not above a or b
    change(run(capsys, "move", a, "--bottom"))
    assert queued_order() == [b, a, d, c]  # a is alone in its band: nothing to do
    doc = change(run(capsys, "move", b, "--top"))
    assert doc["changed"] is False


def test_held_jobs_keep_their_place_and_can_be_anchors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    a, b, c = jobs(capsys, 3)
    sql("UPDATE jobs SET status = 'held', hold_reason = 'manual' WHERE id = ?", b)
    change(run(capsys, "move", c, "--before", b))  # before a held job
    sql("UPDATE jobs SET status = 'queued', hold_reason = NULL WHERE id = ?", b)
    assert queued_order() == [a, c, b]
    # moving a held job changes its stored place for when it is released
    sql("UPDATE jobs SET status = 'held', hold_reason = 'manual' WHERE id = ?", a)
    change(run(capsys, "move", a, "--bottom"))
    sql("UPDATE jobs SET status = 'queued', hold_reason = NULL WHERE id = ?", a)
    assert queued_order() == [c, b, a]


def test_bottom_counts_held_jobs(capsys: pytest.CaptureFixture[str]) -> None:
    a, b = jobs(capsys, 2)
    sql("UPDATE jobs SET status = 'held', hold_reason = 'manual' WHERE id = ?", b)
    doc = change(run(capsys, "move", a, "--bottom"))
    assert doc["changed"] is True  # the held b is below a
    sql("UPDATE jobs SET status = 'queued', hold_reason = NULL WHERE id = ?", b)
    assert queued_order() == [b, a]


def test_move_refusals(capsys: pytest.CaptureFixture[str]) -> None:
    a, b = jobs(capsys, 2)
    other = add(capsys, queue="elsewhere")
    res = run(capsys, "move", a, "--before", other)
    assert res.code == 20
    err = res.envelope()["error"]
    assert (err["code"], err["reason"]) == ("refused", "other_queue")
    sql("UPDATE jobs SET status = 'running' WHERE id = ?", b)
    for argv in (("move", b, "--top"), ("move", a, "--before", b), ("move", b, "--after", a)):
        res = run(capsys, *argv)
        assert res.code == 20, argv
        assert res.envelope()["error"]["reason"] == "not_queued"
    sql("UPDATE jobs SET status = 'done', reason = 'completed' WHERE id = ?", b)
    assert run(capsys, "move", b, "--bottom").code == 20


def test_move_unknown_and_ambiguous_ids(capsys: pytest.CaptureFixture[str]) -> None:
    a, b = jobs(capsys, 2)
    assert run(capsys, "move", "jb-aaaaaaaa", "--top").code == 10
    assert run(capsys, "move", a, "--before", "jb-aaaaaaaa").code == 10
    sql("UPDATE jobs SET id = 'jb-abcdaaaa' WHERE id = ?", a)
    sql("UPDATE jobs SET id = 'jb-abcdbbbb' WHERE id = ?", b)
    res = run(capsys, "move", "abcd", "--top")
    assert res.code == 21
    assert res.envelope()["error"]["candidates"] == ["jb-abcdaaaa", "jb-abcdbbbb"]
    assert run(capsys, "move", "abc", "--top").code == 2


def test_move_writes_a_reorder_log_record(capsys: pytest.CaptureFixture[str]) -> None:
    a, b = jobs(capsys, 2)
    change(run(capsys, "move", b, "--top", "--actor", "human:tester", "--decision-id", "d1"))
    change(run(capsys, "move", b, "--top"))  # no change: no record
    records = [
        r
        for r in validate_record_file(resolve_state_dir() / "log.jsonl", "log.json")
        if r["event"] == "reorder"
    ]
    assert len(records) == 1
    assert records[0]["job_id"] == b and records[0]["queue"] == "q"
    assert records[0]["actor"] == "human:tester" and records[0]["decision_ids"] == ["d1"]
    assert a not in json.dumps(records)


# -- pin, unpin, priority --------------------------------------------------------------------


def test_pin_and_unpin_keep_the_position(capsys: pytest.CaptureFixture[str]) -> None:
    a, b, c = jobs(capsys, 3)
    pos = positions()
    doc = change(run(capsys, "pin", c))
    assert doc["job"]["pinned"] is True and doc["job"]["rank"] == 1
    assert sorted(positions()) == sorted(pos)
    assert queued_order() == [c, a, b]
    change(run(capsys, "pin", a))
    assert queued_order() == [a, c, b]  # lands by age within the pinned group
    doc = change(run(capsys, "unpin", c))
    assert doc["job"]["pinned"] is False
    assert queued_order() == [a, b, c]  # c is back at its age place


def test_pin_noop_and_priority_noop(capsys: pytest.CaptureFixture[str]) -> None:
    a = add(capsys)
    assert change(run(capsys, "unpin", a))["changed"] is False
    change(run(capsys, "pin", a))
    assert change(run(capsys, "pin", a))["changed"] is False
    assert change(run(capsys, "priority", a, "0"))["changed"] is False
    assert change(run(capsys, "priority", a, "1000"))["job"]["priority"] == 1000
    assert change(run(capsys, "priority", a, "-1000"))["job"]["priority"] == -1000
    assert run(capsys, "priority", a, "1001").code == 2


def test_priority_lands_by_age_in_the_new_band(capsys: pytest.CaptureFixture[str]) -> None:
    a, b, c, d = jobs(capsys, 4)
    run(capsys, "priority", b, "2")
    run(capsys, "priority", d, "2")
    assert queued_order() == [b, d, a, c]
    run(capsys, "priority", a, "2")
    assert queued_order() == [a, b, d, c]  # a is the oldest of the band


def test_pin_priority_work_on_held_and_refuse_others(capsys: pytest.CaptureFixture[str]) -> None:
    a, b = jobs(capsys, 2)
    sql("UPDATE jobs SET status = 'held', hold_reason = 'manual' WHERE id = ?", a)
    assert change(run(capsys, "pin", a))["job"]["pinned"] is True
    assert change(run(capsys, "priority", a, "4"))["job"]["priority"] == 4
    sql("UPDATE jobs SET status = 'starting' WHERE id = ?", b)
    for argv in (("pin", b), ("unpin", b), ("priority", b, "1")):
        res = run(capsys, *argv)
        assert res.code == 20 and res.envelope()["error"]["reason"] == "not_queued"


def test_priority_log_record(capsys: pytest.CaptureFixture[str]) -> None:
    a = add(capsys)
    change(run(capsys, "priority", a, "3"))
    change(run(capsys, "pin", a))
    events = [
        r
        for r in validate_record_file(resolve_state_dir() / "log.jsonl", "log.json")
        if r["event"] == "reorder"
    ]
    assert [e["details"]["op"] for e in events] == ["priority", "pin"]


# -- dry run ---------------------------------------------------------------------------------


def snapshot_state() -> tuple[list[tuple[Any, ...]], int]:
    conn = db()
    try:
        rows = [tuple(r) for r in conn.execute("SELECT * FROM jobs ORDER BY id")]
    finally:
        conn.close()
    log = resolve_state_dir() / "log.jsonl"
    return rows, log.stat().st_size


def test_dry_run_projects_and_writes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    a, b, c = jobs(capsys, 3)
    before = snapshot_state()
    doc = change(run(capsys, "move", c, "--before", a, "--dry-run"))
    assert doc["dry_run"] is True and doc["changed"] is True
    assert doc["job"]["rank"] == 1
    doc = change(run(capsys, "pin", b, "--dry-run"))
    assert doc["job"]["pinned"] is True and doc["job"]["rank"] == 1
    doc = change(run(capsys, "priority", a, "9", "--dry-run"))
    assert doc["job"]["priority"] == 9 and doc["job"]["rank"] == 1
    doc = change(run(capsys, "unpin", a, "--dry-run"))
    assert doc["changed"] is False and doc["dry_run"] is True
    assert snapshot_state() == before
    assert queued_order() == [a, b, c]


def test_dry_run_projected_rank_ignores_the_stale_row(capsys: pytest.CaptureFixture[str]) -> None:
    a, b, c = jobs(capsys, 3)
    doc = change(run(capsys, "move", a, "--after", c, "--dry-run"))
    assert doc["job"]["rank"] == 3
    doc = change(run(capsys, "move", c, "--top", "--dry-run"))
    assert doc["job"]["rank"] == 1


def test_dry_run_checks_still_apply(capsys: pytest.CaptureFixture[str]) -> None:
    a, b = jobs(capsys, 2)
    sql("UPDATE jobs SET status = 'running' WHERE id = ?", b)
    assert run(capsys, "move", b, "--top", "--dry-run").code == 20
    assert run(capsys, "move", "jb-aaaaaaaa", "--top", "--dry-run").code == 10


def test_dry_run_without_a_state_directory_is_not_found(
    capsys: pytest.CaptureFixture[str],
) -> None:
    res = run(capsys, "pin", "jb-aaaaaaaa", "--dry-run")
    assert res.code == 10
    assert not (resolve_state_dir()).exists()


def seed(positions_by_id: dict[str, str]) -> None:
    for job_id, position in positions_by_id.items():
        sql("UPDATE jobs SET position = ? WHERE id = ?", position, job_id)


def dry_then_real(capsys: pytest.CaptureFixture[str], *argv: str) -> None:
    """The dry run exits 0, writes nothing and projects what the real command then yields."""
    before = snapshot_state()
    dry = change(run(capsys, *argv, "--dry-run"))
    assert snapshot_state() == before
    real = change(run(capsys, *argv))
    assert dry["dry_run"] is True and real["dry_run"] is False
    assert dry["changed"] == real["changed"]
    assert dry["job"]["rank"] == real["job"]["rank"]


def test_dry_run_move_with_a_key_over_the_length_limit(capsys: pytest.CaptureFixture[str]) -> None:
    a, b, c = jobs(capsys, 3)
    base = integer_key(10**15)
    seed({a: base, b: base + "." + "0" * 60 + "1", c: integer_key(10**15 + 5)})
    dry_then_real(capsys, "move", c, "--after", a)
    assert queued_order() == [a, c, b]


def test_dry_run_move_with_equal_keys(capsys: pytest.CaptureFixture[str]) -> None:
    a, b, c = jobs(capsys, 3)
    lo, hi = sorted([a, b])  # equal keys tie-break by id, so moving before `hi` must renumber
    same = integer_key(10**15)
    seed({a: same, b: same, c: integer_key(10**15 + 5)})
    dry_then_real(capsys, "move", c, "--before", hi)
    assert queued_order() == [lo, c, hi]
    assert len(set(positions())) == 3  # the tie was broken, so the renumber path ran


def test_dry_run_top_at_integer_part_zero(capsys: pytest.CaptureFixture[str]) -> None:
    a, b, c = jobs(capsys, 3)
    seed({a: "0" * 20, b: "0" * 19 + "1", c: "0" * 19 + "2"})
    dry_then_real(capsys, "move", c, "--top")
    assert queued_order() == [c, a, b]


def test_dry_run_bottom_at_the_last_integer_part(capsys: pytest.CaptureFixture[str]) -> None:
    a, b = jobs(capsys, 2)
    seed({a: "9" * 20, b: "9" * 19 + "8"})
    dry_then_real(capsys, "move", b, "--bottom")
    assert queued_order() == [a, b]


def test_noop_move_does_not_renumber(capsys: pytest.CaptureFixture[str]) -> None:
    a, b, c = jobs(capsys, 3)
    same = integer_key(10**15)
    seed({a: same, b: same, c: integer_key(10**15 + 5)})
    first, second = sorted([a, b])
    before = snapshot_state()
    doc = change(run(capsys, "move", second, "--after", first))
    assert doc["changed"] is False
    assert snapshot_state() == before


def test_cross_band_collision_at_zero_renumbers_instead_of_overflowing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    a, z, x = jobs(capsys, 3)
    change(run(capsys, "priority", z, "5"))
    seed({a: "0" * 19 + "1", z: "0" * 20, x: "0" * 19 + "5"})
    change(run(capsys, "move", x, "--before", a))
    band = [j for j in queued_order() if j != z]
    assert band == [x, a]
    assert len(set(positions())) == 3


# -- many moves ------------------------------------------------------------------------------


def test_repeated_moves_between_the_same_neighbours(capsys: pytest.CaptureFixture[str]) -> None:
    ids = jobs(capsys, 4)
    first, last = ids[0], ids[-1]
    middle = ids[1:3]
    model = list(ids)
    for i in range(150):
        job_id = middle[i % 2]
        change(run(capsys, "move", job_id, "--after", first))
        model.remove(job_id)
        model.insert(1, job_id)
        assert queued_order() == model
    assert model[0] == first and model[-1] == last
    assert len(set(positions())) == 4


def test_key_growth_is_bounded_by_renumbering(capsys: pytest.CaptureFixture[str]) -> None:
    a, b, c = jobs(capsys, 3)
    for i in range(400):
        change(run(capsys, "move", b if i % 2 else c, "--after", a))
    assert queued_order()[0] == a
    assert max(len(p) for p in positions()) < 100
    assert len(set(positions())) == 3


def read_state() -> dict[str, tuple[int, int, str]]:
    conn = db()
    try:
        return {
            r["id"]: (r["pinned"], r["priority"], r["position"])
            for r in conn.execute("SELECT * FROM jobs WHERE status IN ('queued', 'held')")
        }
    finally:
        conn.close()


def bands(state: dict[str, tuple[int, int, str]]) -> dict[tuple[int, int], list[str]]:
    found: dict[tuple[int, int], list[str]] = {}
    for job_id, (pinned, priority, _position) in sorted(
        state.items(), key=lambda kv: (kv[1][2], kv[0])
    ):
        found.setdefault((pinned, priority), []).append(job_id)
    return found


def test_random_operations_match_a_band_model(capsys: pytest.CaptureFixture[str]) -> None:
    """After every command the bands hold exactly what the spec says: a move puts the job
    directly before/after the other in the other's band, top/bottom end its own band, and
    pin/unpin/priority keep the stored key (so the job lands by position in the new band)."""
    rng = random.Random(20260101)
    ids = jobs(capsys, 7)
    for step in range(250):
        before = read_state()
        old = bands(before)
        op = rng.choice(["before", "after", "top", "bottom", "pin", "unpin", "prio"])
        job_id = rng.choice(ids)
        band = before[job_id][:2]
        expected = {k: list(v) for k, v in old.items()}
        if op in ("before", "after"):
            other = rng.choice([i for i in ids if i != job_id])
            change(run(capsys, "move", job_id, f"--{op}", other))
            expected[band].remove(job_id)
            target = before[other][:2]
            row = expected.setdefault(target, [])
            at = row.index(other) + (0 if op == "before" else 1)
            row.insert(at, job_id)
        elif op in ("top", "bottom"):
            change(run(capsys, "move", job_id, f"--{op}"))
            row = expected[band]
            row.remove(job_id)
            row.insert(0 if op == "top" else len(row), job_id)
        else:
            if op == "prio":
                value = rng.choice([-5, 0, 0, 3, 3, 9])
                change(run(capsys, "priority", job_id, str(value)))
                new_band = (band[0], value)
            else:
                change(run(capsys, op, job_id))
                new_band = (1 if op == "pin" else 0, band[1])
            expected[band].remove(job_id)
            row = expected.setdefault(new_band, [])
            row.append(job_id)
            row.sort(key=lambda j: (before[j][2], j))
        after = read_state()
        got = bands(after)
        assert got == {k: v for k, v in expected.items() if v}, f"step {step}: {op}"
        if op in ("pin", "unpin", "prio"):
            assert after[job_id][2] == before[job_id][2]
        assert len({v[2] for v in after.values()}) == len(ids), f"step {step}: duplicate key"
        assert queued_order() == [
            j for k in sorted(got, key=lambda k: (-k[0], -k[1])) for j in got[k]
        ]


# -- concurrency -----------------------------------------------------------------------------


def jobsctl_cmd(*argv: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "jobsctl", "--json", *argv],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ),
        cwd="/",
    )


def test_concurrent_moves_and_adds_keep_keys_unique(capsys: pytest.CaptureFixture[str]) -> None:
    ids = jobs(capsys, 4)
    procs: list[subprocess.Popen[str]] = []
    for i in range(12):
        procs.append(jobsctl_cmd("move", ids[i % 4], "--after", ids[(i + 1) % 4]))
        procs.append(
            jobsctl_cmd("add", "--no-start", "--queue", "q", "--top", "--cwd", "/w", "--", "true")
        )
        procs.append(jobsctl_cmd("add", "--no-start", "--queue", "q", "--cwd", "/w", "--", "true"))
    try:
        results = [(p.communicate(timeout=120), p.returncode) for p in procs]
    finally:
        for p in procs:  # kill exactly the processes this test started, if any is still alive
            if p.poll() is None:
                p.kill()
                p.wait()
    assert [rc for _, rc in results] == [0] * len(procs), [out for out, rc in results if rc]
    keys = positions()
    assert len(keys) == 4 + 24 and len(set(keys)) == len(keys)
    assert len(queued_order()) == len(keys)


# -- capacity --------------------------------------------------------------------------------


def capacity(capsys: pytest.CaptureFixture[str], *flags: str, config: str | None = None) -> Any:
    res = run(capsys, "capacity", *flags, config=config)
    assert res.code == 0, res.out + res.err
    doc = res.envelope()
    validate_against("cli/capacity.json", doc)
    return doc


def test_capacity_unknown_queue_answers_with_default_cap(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = write_config(tmp_path, default_cap=3)
    doc = capacity(capsys, "--queue", "nothing", config=cfg)
    assert doc["queues"] == [
        {
            "queue": "nothing",
            "known": False,
            "cap": 3,
            "running": 0,
            "queued": 0,
            "free": 3,
            "effective_free": 3,
        }  # fmt: skip
    ]
    assert doc["global"] == {"cap": None, "running": 0, "queued": 0, "free": None}


def test_capacity_counts_jobs_just_added(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = write_config(tmp_path, default_cap=3)
    for expected in (2, 1, 0, 0):
        add(capsys, queue="w")
        got = capacity(capsys, "--queue", "w", config=cfg)["queues"][0]
        assert got["free"] == got["effective_free"] == expected
        assert got["known"] is True


def test_capacity_running_queued_held_and_waiting(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = write_config(tmp_path, default_cap=10)
    a, b, c, d, e = jobs(capsys, 5)
    sql("UPDATE jobs SET status = 'running' WHERE id = ?", a)
    sql("UPDATE jobs SET status = 'starting' WHERE id = ?", b)
    sql("UPDATE jobs SET status = 'held', hold_reason = 'manual' WHERE id = ?", c)
    sql("UPDATE jobs SET not_before = '2999-01-01T00:00:00.000Z' WHERE id = ?", d)
    got = capacity(capsys, "--queue", "q", config=cfg)["queues"][0]
    assert (got["running"], got["queued"], got["free"]) == (2, 2, 6)  # held is not counted
    # the waiting job counts as queued for the queue, but is not ready for the global entry
    assert capacity(capsys, config=cfg)["global"]["queued"] == 1
    del e


def test_capacity_null_caps_and_global_cap(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = write_config(tmp_path, default_cap=None, global_cap=3, queues={"cfg": {"cap": 5}})
    jobs(capsys, 2, queue="free")
    doc = capacity(capsys, config=cfg)
    by = {q["queue"]: q for q in doc["queues"]}
    assert by["free"]["cap"] is None and by["free"]["free"] is None
    assert by["free"]["effective_free"] == 1  # global: 3 - 2 ready
    assert by["cfg"]["known"] is True and by["cfg"]["effective_free"] == 1  # min(5, 3 - 2)
    assert doc["global"] == {"cap": 3, "running": 0, "queued": 2, "free": 1}


def test_capacity_effective_free_never_above_free(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = write_config(tmp_path, default_cap=2, global_cap=100)
    jobs(capsys, 1, queue="a")
    for q in capacity(capsys, config=cfg)["queues"]:
        assert q["effective_free"] <= q["free"]


def test_capacity_paused_by_cap_zero(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    cfg = write_config(tmp_path, queues={"p": {"cap": 0}}, global_cap=0)
    doc = capacity(capsys, "--queue", "p", config=cfg)
    assert doc["queues"][0]["free"] == 0 and doc["queues"][0]["effective_free"] == 0
    assert doc["queues"][0]["known"] is True  # configured


def test_capacity_with_no_state_creates_nothing_surprising(
    capsys: pytest.CaptureFixture[str],
) -> None:
    doc = capacity(capsys)
    assert doc["queues"] == []
    assert doc["global"]["running"] == 0


def test_capacity_lowered_cap_below_running(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = write_config(tmp_path, default_cap=1)
    a, b = jobs(capsys, 2)
    sql("UPDATE jobs SET status = 'running'")
    got = capacity(capsys, "--queue", "q", config=cfg)["queues"][0]
    assert (got["running"], got["free"], got["effective_free"]) == (2, 0, 0)


# -- queues ----------------------------------------------------------------------------------


def test_queues_lists_known_queues_with_counts(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = write_config(
        tmp_path,
        queues={"cfg": {"cap": 4, "priority": 2, "timeout": "5m"}},
        hooks={"pre-start": [{"name": "guard", "cmd": "true", "queues": ["ghost", "cfg", "used"]}]},
    )
    a, b = jobs(capsys, 2, queue="used")
    sql("UPDATE jobs SET status = 'running' WHERE id = ?", a)
    res = run(capsys, "queues", config=cfg)
    assert res.code == 0, res.out + res.err
    doc = res.envelope()
    validate_against("cli/queues.json", doc)
    names = [q["queue"] for q in doc["queues"]]
    assert names == ["cfg", "used"]
    cfgq, used = doc["queues"]
    assert (cfgq["configured"], cfgq["cap"], cfgq["priority"], cfgq["timeout"]) == (True, 4, 2, 300)
    assert cfgq["first_seen"] is None and cfgq["counts"]["queued"] == 0
    assert used["configured"] is False and used["first_seen"] is not None
    assert used["counts"]["running"] == 1 and used["counts"]["queued"] == 1
    assert doc["unmatched_selectors"] == [
        {"hook": "pre-start", "hook_id": "guard", "queue": "ghost"}
    ]


def test_queues_empty(capsys: pytest.CaptureFixture[str]) -> None:
    res = run(capsys, "queues")
    assert res.code == 0
    assert res.envelope() == {"queues": [], "unmatched_selectors": []}


# -- snapshot --------------------------------------------------------------------------------


def test_load_snapshot_feeds_selection(capsys: pytest.CaptureFixture[str]) -> None:
    from jobsctl.config import Config
    from jobsctl.order import load_snapshot, select_next
    from jobsctl.store_db import OpenMode, Store

    plain = add(capsys)
    prio = add(capsys, "--priority", "5")
    pinned = add(capsys)
    change(run(capsys, "pin", pinned))
    env = add(capsys, "--pass-env", "TOK", queue="e")
    sql("UPDATE jobs SET status = 'running' WHERE id = ?", plain)
    other = add(capsys)
    with Store.open(busy_timeout=1000, mode=OpenMode.CLI) as store, store.read() as tx:
        snap = load_snapshot(tx, Config(default_cap=None))
    by_name = {q.name: q for q in snap.queues}
    assert by_name["q"].running == 1
    assert [c.id for c in by_name["q"].queued] == [pinned, prio, other]
    assert [c.needs_env_file for c in by_name["e"].queued] == [True]
    assert by_name["e"].queued[0].id == env
    now = "2999-01-01T00:00:00.000Z"
    sel = select_next(snap, now=now, env_file_unavailable=True)
    assert sel is not None and (sel.queue, sel.job_id) == ("q", pinned)
    sql("UPDATE jobs SET status = 'held' WHERE queue = 'q'")
    with Store.open(busy_timeout=1000, mode=OpenMode.CLI) as store, store.read() as tx:
        snap = load_snapshot(tx, Config(default_cap=None))
    assert select_next(snap, now=now, env_file_unavailable=True) is None
    sel = select_next(snap, now=now, env_file_unavailable=False)
    assert sel is not None and (sel.queue, sel.job_id) == ("e", env)
