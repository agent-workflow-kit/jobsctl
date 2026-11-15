"""The shim's start sequence, exit mapping and final commit, in process (real job processes)."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from jobsctl.config import Config
from jobsctl.errors import JobsctlError
from jobsctl.store_db import Store
from tests.conftest import validate_against
from tests.shim_helpers import (
    JOB,
    attempt,
    environ,
    exit_of,
    job,
    log_records,
    make_shim,
    one,
    seed_claimed,
    shims_dir,
    tail_of,
)


@pytest.fixture
def store() -> Iterator[Store]:
    from jobsctl.store_db import OpenMode

    with Store.open(mode=OpenMode.CLI) as s:
        yield s


@pytest.fixture(autouse=True)
def _validated(validated_records: None) -> None:
    return None


def run_job(store: Store, argv: list[str], **seed: object) -> int:
    seed_claimed(store, argv, **seed)  # type: ignore[arg-type]
    return make_shim(store).run()


# -- the happy path and the exit mapping ----------------------------------------------------------


def test_a_job_runs_to_done_with_its_output_identity_and_bookkeeping(store: Store) -> None:
    assert run_job(store, ["sh", "-c", "echo hello; echo oops >&2"]) == 0
    row, att = job(store), attempt(store)
    assert (row["status"], row["reason"], row["hold_reason"]) == ("done", "exit", None)
    assert row["ended_at"] is not None and row["spawn_failures"] == 0
    assert (att["status"], att["reason"]) == ("done", "exit")
    assert exit_of(store) == {"kind": "exited", "code": 0, "signal": None, "core": False}
    assert att["resolved_path"] in ("/usr/bin/sh", "/bin/sh")
    assert att["shim_confirmed_at"] is not None and att["running_at"] is not None
    assert att["leftovers"] == 0
    assert json.loads(att["effective"]) == {"timeout": None, "max_retries": 0, "kill_grace": 30}
    assert att["job_identity"] and att["pgid"] and att["ended_at"]
    assert tail_of(store) == "hello\noops\n"  # one pipe: stdout and stderr interleaved in order
    assert store.query_one("SELECT 1 FROM leases WHERE name = ?", [f"job:{JOB}"]) is None
    assert list(shims_dir().glob("*.result*")) == []  # removed after the final commit


@pytest.mark.parametrize(
    ("code", "status", "reason", "hold"),
    [
        ("0", "done", "exit", None),
        ("1", "failed", "exit", None),
        ("13", "held", None, "exit_13"),
        ("143", "failed", "exit", None),
        ("255", "failed", "exit", None),
    ],
)
def test_exit_codes_map_to_statuses(
    store: Store, code: str, status: str, reason: str | None, hold: str | None
) -> None:
    run_job(store, ["sh", "-c", f"exit {code}"])
    row = job(store)
    assert (row["status"], row["reason"], row["hold_reason"]) == (status, reason, hold)
    assert attempt(store)["status"] == status
    assert (row["ended_at"] is None) == (status == "held")  # a hold is not terminal
    assert attempt(store)["ended_at"] is not None
    assert exit_of(store)["code"] == int(code)  # type: ignore[index]


def test_death_by_a_signal_the_shim_did_not_send_is_signaled(store: Store) -> None:
    run_job(store, ["sh", "-c", "kill -KILL $$"])
    row = job(store)
    assert (row["status"], row["reason"]) == ("failed", "signaled")
    assert exit_of(store) == {"kind": "signaled", "code": None, "signal": 9, "core": False}


def test_a_core_dump_is_reported(store: Store) -> None:
    run_job(store, ["sh", "-c", "ulimit -c unlimited 2>/dev/null; kill -SEGV $$"])
    exit_ = exit_of(store)
    assert exit_ is not None and exit_["kind"] == "signaled" and exit_["signal"] == 11


def test_the_job_runs_in_its_own_process_group_with_dev_null_stdin(store: Store) -> None:
    run_job(
        store,
        [
            sys.executable, "-I", "-c",
            "import os, sys; print(os.getpgrp() == os.getpid(), os.getsid(0) != os.getpid(),"
            " sys.stdin.read() == '')",
        ],
    )  # fmt: skip
    assert tail_of(store) == "True True True\n"
    att = attempt(store)
    assert att["pgid"] == int(att["job_identity"].rsplit(":", 2)[1])  # pgid == the leader's PID


def test_the_transitions_are_logged_in_order_and_validate(store: Store) -> None:
    run_job(store, ["true"])
    pairs = [(r["from"], r["to"]) for r in log_records("transition")]
    assert pairs == [("starting", "running"), ("running", "done")]


def test_a_pass_is_requested_after_the_final_commit(
    store: Store, trigger_spawns: list[dict[str, object]]
) -> None:
    run_job(store, ["true"])
    assert len(trigger_spawns) == 1
    with store.read() as tx:
        row = tx.one("SELECT value FROM counters WHERE name = 'pass_requested'")
    assert row is not None and row["value"] == 1


def test_auto_run_false_requests_nothing(
    store: Store, trigger_spawns: list[dict[str, object]]
) -> None:
    seed_claimed(store, ["true"])
    make_shim(store, Config.model_validate({"auto_run": False})).run()
    assert trigger_spawns == [] and job(store)["status"] == "done"


def test_the_post_events_are_written_with_the_transitions(store: Store) -> None:
    cfg = Config.model_validate(
        {"hooks": {"post-start": [{"cmd": "true"}], "post-finish": [{"cmd": "true"}]}}
    )
    seed_claimed(store, ["true"])
    make_shim(store, cfg).run()
    rows = store.query_all("SELECT hook, payload FROM hook_outbox ORDER BY seq")
    assert [r["hook"] for r in rows] == ["post-start", "post-finish"]
    finish = json.loads(rows[1]["payload"])
    assert finish["previous_status"] == "running" and finish["job"]["status"] == "done"
    assert finish["attempt"]["exit"]["code"] == 0 and finish["output_available"] is True
    for row in rows:
        validate_against("hooks/payload.json", json.loads(row["payload"]))


def test_a_post_fail_event_for_a_failed_exit_and_post_held_for_exit_13(store: Store) -> None:
    cfg = Config.model_validate(
        {"hooks": {"post-fail": [{"cmd": "true"}], "post-held": [{"cmd": "true"}]}}
    )
    seed_claimed(store, ["sh", "-c", "exit 13"])
    make_shim(store, cfg).run()
    assert [r["hook"] for r in store.query_all("SELECT hook FROM hook_outbox")] == ["post-held"]


# -- failures before the job starts ---------------------------------------------------------------


def test_a_missing_cwd_fails_cwd_missing_with_no_exit_record(store: Store, tmp_path: Path) -> None:
    run_job(store, ["true"], cwd=str(tmp_path / "gone"))
    row = job(store)
    assert (row["status"], row["reason"]) == ("failed", "cwd_missing")
    assert attempt(store)["exit"] is None and attempt(store)["resolved_path"] is None


def test_a_cwd_that_is_a_file_fails_cwd_missing(store: Store, tmp_path: Path) -> None:
    f = tmp_path / "file"
    f.write_text("x")
    run_job(store, ["true"], cwd=str(f))
    assert job(store)["reason"] == "cwd_missing"


def test_an_unresolvable_argv0_fails_spawn_failed_with_a_null_path(store: Store) -> None:
    run_job(store, ["no-such-command-xyz"])
    row = job(store)
    assert (row["status"], row["reason"]) == ("failed", "spawn_failed")
    assert exit_of(store) == {"kind": "spawn_failed", "code": None, "signal": None, "core": False}
    assert attempt(store)["resolved_path"] is None


def test_an_exec_failure_keeps_the_resolved_path(store: Store, tmp_path: Path) -> None:
    script = tmp_path / "bad"
    script.write_text("#!/nonexistent/interpreter\n")
    script.chmod(0o755)
    run_job(store, [str(script)])
    row = job(store)
    assert (row["status"], row["reason"]) == ("failed", "spawn_failed")
    assert attempt(store)["resolved_path"] == str(script)
    assert exit_of(store)["kind"] == "spawn_failed"  # type: ignore[index]
    assert attempt(store)["running_at"] is None


def test_a_non_executable_file_fails_spawn_failed(store: Store, tmp_path: Path) -> None:
    f = tmp_path / "noexec"
    f.write_text("echo hi\n")
    f.chmod(0o644)
    run_job(store, [str(f)])
    assert job(store)["reason"] == "spawn_failed" and attempt(store)["resolved_path"] is None


def test_a_missing_pass_env_variable_fails_missing_env_naming_it_in_the_log(store: Store) -> None:
    run_job(store, ["true"], pass_env=["NOT_THERE"])
    assert job(store)["reason"] == "missing_env" and attempt(store)["exit"] is None
    (rec,) = log_records("missing_env")
    assert rec["details"]["variable"] == "NOT_THERE"


def test_an_unreadable_config_fails_spawn_failed_and_logs_config_invalid(store: Store) -> None:
    seed_claimed(store, ["true"])
    make_shim(store, config_error=JobsctlError("config_invalid", "bad config")).run()
    row = job(store)
    assert (row["status"], row["reason"]) == ("failed", "spawn_failed")
    assert attempt(store)["exit"] is None and attempt(store)["effective"] is None
    assert [r["event"] for r in log_records("config_invalid")] == ["config_invalid"]


# -- the job environment --------------------------------------------------------------------------


def test_home_is_absent_when_not_inherited_and_the_job_ids_are_set(store: Store) -> None:
    cfg = Config.model_validate({"env": {"inherit": ["PATH"]}})
    seed_claimed(store, ["env"], env={"MY": "v"})
    make_shim(store, cfg, env=environ(SECRET_LOOKING="zzz")).run()
    lines = (tail_of(store) or "").splitlines()
    names = {line.split("=", 1)[0] for line in lines}
    assert "HOME" not in names and "XDG_STATE_HOME" not in names
    assert {"PATH", "MY", "JOBSCTL_JOB_ID", "JOBSCTL_ATTEMPT"} <= names
    assert "SECRET_LOOKING" not in names and "LANG" not in names
    assert "JOBSCTL_JOB_ID=" + JOB in lines and "JOBSCTL_ATTEMPT=1" in lines


def test_home_is_inherited_by_default_but_never_a_jobsctl_variable(store: Store) -> None:
    seed_claimed(store, ["env"])
    make_shim(store, env=environ(JOBSCTL_FOO="1", HOME="/home/x")).run()
    lines = (tail_of(store) or "").splitlines()
    assert "HOME=/home/x" in lines and not any(line.startswith("JOBSCTL_FOO") for line in lines)


def test_env_set_and_job_env_override_in_order(store: Store) -> None:
    cfg = Config.model_validate({"env": {"set": {"A": "set", "B": "set"}}})
    seed_claimed(store, ["env"], env={"B": "job"})
    make_shim(store, cfg).run()
    lines = (tail_of(store) or "").splitlines()
    assert "A=set" in lines and "B=job" in lines


@pytest.fixture
def secret_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """An `env_file` the trust checks accept (the temp dir sits under a world-writable /tmp, so
    the test seam trusts the ancestors up to it)."""
    from functools import partial

    from jobsctl import shim as shim_module
    from jobsctl.env_file import load_env_file

    d = tmp_path / "secrets"
    d.mkdir(mode=0o700)
    f = d / "env"
    f.write_text("TOKEN=supersecretvalue123\nSHORT=abc\n")
    f.chmod(0o600)
    monkeypatch.setattr(
        shim_module, "load_env_file", partial(load_env_file, check_ancestors_from=tmp_path)
    )
    return str(f)


def test_pass_env_comes_from_the_env_file_and_the_output_is_masked(
    store: Store, secret_env_file: str
) -> None:
    cfg = Config.model_validate({"env_file": secret_env_file})
    seed_claimed(store, ["sh", "-c", 'echo "token=$TOKEN"; echo "n=${SHORT}"'], pass_env=["TOKEN"])
    make_shim(store, cfg).run()
    text = tail_of(store) or ""
    assert "supersecretvalue123" not in text and "token=***" in text
    assert job(store)["status"] == "done"
    # a short secret is not masked and says so in the log, naming the variable, never the value
    (warning,) = log_records("short_secret_not_masked")
    assert warning["details"]["variable"] == "SHORT"
    assert (
        "supersecretvalue123"
        not in (Path(os.environ["XDG_STATE_HOME"]) / "jobsctl" / "log.jsonl").read_text()
    )


def test_env_file_values_reach_a_job_only_through_pass_env(
    store: Store, secret_env_file: str
) -> None:
    cfg = Config.model_validate({"env_file": secret_env_file})
    seed_claimed(store, ["env"])
    make_shim(store, cfg).run()
    assert "TOKEN" not in (tail_of(store) or "")


def test_an_unsafe_env_file_makes_pass_env_unavailable(store: Store, tmp_path: Path) -> None:
    f = tmp_path / "open-env"
    f.write_text("TOKEN=supersecretvalue123\n")
    f.chmod(0o644)  # readable by others: unavailable
    cfg = Config.model_validate({"env_file": str(f)})
    seed_claimed(store, ["true"], pass_env=["TOKEN"])
    make_shim(store, cfg).run()
    assert job(store)["reason"] == "missing_env"


# -- identity, the claim and cancel races --------------------------------------------------------


def test_a_stale_claim_id_writes_nothing(store: Store) -> None:
    seed_claimed(store, ["true"])
    before = one(store, "SELECT * FROM attempts")
    assert make_shim(store, claim_id="claim-other").run() == 0
    assert dict(one(store, "SELECT * FROM attempts")) == dict(before)
    assert job(store)["status"] == "starting"


def test_a_different_recorded_identity_exits_without_writing(store: Store) -> None:
    seed_claimed(store, ["true"], shim_identity="boot:2:3")
    assert make_shim(store).run() == 0
    assert job(store)["status"] == "starting" and attempt(store)["shim_confirmed_at"] is None


def test_no_recorded_identity_and_a_dead_claimer_exits(store: Store) -> None:
    seed_claimed(store, ["true"], shim_identity="", claimed_by="no-such-boot:99999:1")
    assert make_shim(store).run() == 0
    assert attempt(store)["shim_confirmed_at"] is None and job(store)["status"] == "starting"


def test_the_shim_polls_while_the_claiming_pass_is_alive_and_has_not_recorded_it(
    store: Store, sleeper: str
) -> None:
    from jobsctl.leases import own_identity
    from jobsctl.proc_host import default_host

    seed_claimed(store, ["true"], shim_identity="", claimed_by=sleeper)
    mine = own_identity(default_host())

    def record() -> None:
        time.sleep(0.5)
        with Store.open() as other:
            with other.write() as tx:
                tx.execute("UPDATE attempts SET shim_identity = ? WHERE job_id = ?", [mine, JOB])

    t = threading.Thread(target=record)
    t.start()
    make_shim(store).run()
    t.join()
    assert job(store)["status"] == "done"


def test_a_canceled_starting_job_is_never_spawned(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_claimed(store, ["true"])
    spawned: list[object] = []
    from jobsctl import gateway

    real = gateway.spawn_job
    monkeypatch.setattr(gateway, "spawn_job", lambda *a, **k: spawned.append(1) or real(*a, **k))
    with store.write() as tx:  # the cancel committed before the shim looked again
        tx.execute(
            "UPDATE jobs SET status = 'canceled', reason = 'canceled', ended_at = ? WHERE id = ?",
            ["2026-01-01T00:00:00.000Z", JOB],
        )
    make_shim(store).run()
    assert spawned == [] and job(store)["status"] == "canceled"
    assert attempt(store)["running_at"] is None


def test_a_cancel_between_the_checks_and_the_start_transaction_wins(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cancel commits after cwd, environment and `argv[0]` were checked but before the start
    transaction: the transaction re-reads the job and spawns nothing."""
    seed_claimed(store, ["true"])
    from jobsctl import gateway
    from jobsctl import shim as shim_module

    spawned: list[object] = []
    monkeypatch.setattr(gateway, "spawn_job", lambda *a, **k: spawned.append(1))
    real = shim_module.resolve_argv0

    def resolve_then_cancel(*args: object, **kwargs: object) -> str | None:
        out = real(*args, **kwargs)  # type: ignore[arg-type]
        with Store.open() as other, other.write() as tx:
            tx.execute(
                "UPDATE jobs SET status = 'canceled', reason = 'canceled' WHERE id = ?", [JOB]
            )
        return out

    monkeypatch.setattr(shim_module, "resolve_argv0", resolve_then_cancel)
    make_shim(store).run()
    assert spawned == []
    row = job(store)
    assert (row["status"], row["reason"]) == ("canceled", "canceled")


def test_a_cancel_after_the_start_transaction_sees_running(store: Store) -> None:
    """The mirror of the race: once `running` is committed a cancel sees it (it is refused
    without `--force`), and the job runs to its own end."""
    seed_claimed(store, ["sleep", "1"])
    seen: list[str] = []

    def watch() -> None:
        with Store.open() as other:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                row = one(other, "SELECT status FROM jobs WHERE id = ?", [JOB])
                if row["status"] == "running":
                    seen.append("running")
                    return
                time.sleep(0.02)

    t = threading.Thread(target=watch)
    t.start()
    make_shim(store).run()
    t.join()
    assert seen == ["running"] and job(store)["status"] == "done"


def test_the_job_lease_wait_writes_nothing_and_ends_on_sigterm(store: Store, sleeper: str) -> None:
    from jobsctl.store_ops import take_lease

    seed_claimed(store, ["true"])
    assert take_lease(
        store, f"job:{JOB}", sleeper, now="2026-01-01T00:00:00.000Z",
        expires_at="2099-01-01T00:00:00.000Z", is_alive=lambda h: None,
    )  # fmt: skip
    shim = make_shim(store)
    t = threading.Timer(0.6, shim.request_stop)
    t.start()
    started = time.monotonic()
    assert shim.run() == 0
    t.join()
    assert time.monotonic() - started >= 0.5
    assert job(store)["status"] == "starting" and attempt(store)["effective"] is None
    lease = one(store, "SELECT holder_identity FROM leases WHERE name = ?", [f"job:{JOB}"])
    assert lease["holder_identity"] == sleeper  # not stolen, not released


def test_the_shim_waits_for_a_live_lease_holder_then_proceeds(store: Store, sleeper: str) -> None:
    from jobsctl.store_ops import lease_release, take_lease

    seed_claimed(store, ["true"])
    take_lease(
        store, f"job:{JOB}", sleeper, now="2026-01-01T00:00:00.000Z",
        expires_at="2099-01-01T00:00:00.000Z", is_alive=lambda h: None,
    )  # fmt: skip

    def release() -> None:
        time.sleep(0.6)
        with Store.open() as other, other.write() as tx:
            lease_release(tx, f"job:{JOB}", sleeper)

    t = threading.Thread(target=release)
    t.start()
    make_shim(store).run()
    t.join()
    assert job(store)["status"] == "done"


def test_sigterm_after_the_lease_ends_a_starting_job_terminated(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobsctl import shim as shim_module

    seed_claimed(store, ["true"])
    shim = make_shim(store)
    real = shim_module.record_effective

    def record_then_term(*args: object, **kwargs: object) -> bool:
        out = real(*args, **kwargs)  # type: ignore[arg-type]
        shim.request_stop()  # SIGTERM during the (future) pre-start hooks
        return out

    monkeypatch.setattr(shim_module, "record_effective", record_then_term)
    shim.run()
    row = job(store)
    assert (row["status"], row["reason"]) == ("failed", "terminated")
    assert attempt(store)["exit"] is None and attempt(store)["running_at"] is None


# -- confirm resets the spawn failures ------------------------------------------------------------


def test_confirming_resets_the_spawn_failure_count(store: Store) -> None:
    seed_claimed(store, ["sleep", "0.2"], spawn_failures=2)
    seen: list[int] = []

    def watch() -> None:
        with Store.open() as other:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                row = one(other, "SELECT status, spawn_failures FROM jobs WHERE id = ?", [JOB])
                if row["status"] == "running":
                    seen.append(row["spawn_failures"])
                    return
                time.sleep(0.01)

    t = threading.Thread(target=watch)
    t.start()
    make_shim(store).run()
    t.join()
    assert seen == [0]


# -- the force request ----------------------------------------------------------------------------


def test_a_committed_force_request_makes_the_end_canceled_forced_whatever_the_exit(
    store: Store,
) -> None:
    seed_claimed(store, ["sleep", "1.5"])

    def force() -> None:
        with Store.open() as other:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                row = one(other, "SELECT status FROM jobs WHERE id = ?", [JOB])
                if row["status"] == "running":
                    with other.write() as tx:
                        tx.execute(
                            "UPDATE attempts SET force_requested_by = 'human:me' WHERE job_id = ?",
                            [JOB],
                        )
                    return
                time.sleep(0.01)

    t = threading.Thread(target=force)
    t.start()
    make_shim(store).run()
    t.join()
    row = job(store)
    assert (row["status"], row["reason"]) == ("canceled", "forced")
    assert exit_of(store)["kind"] == "exited"  # type: ignore[index]  # the observed exit stays
    assert attempt(store)["force_requested_by"] == "human:me"


def test_a_job_canceled_while_the_shim_waits_for_the_lease_is_never_started(
    store: Store, sleeper: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancel racing the start sequence: the shim confirmed, waits for `job:<id>`, the job is
    canceled, the lease is released; the shim re-reads, spawns nothing and exits."""
    from jobsctl import gateway
    from jobsctl.store_ops import lease_release, take_lease

    spawned: list[object] = []
    monkeypatch.setattr(gateway, "spawn_job", lambda *a, **k: spawned.append(1))
    seed_claimed(store, ["true"])
    take_lease(
        store, f"job:{JOB}", sleeper, now="2026-01-01T00:00:00.000Z",
        expires_at="2099-01-01T00:00:00.000Z", is_alive=lambda h: None,
    )  # fmt: skip

    def cancel_then_release() -> None:
        time.sleep(0.5)
        with Store.open() as other, other.write() as tx:
            tx.execute(
                "UPDATE jobs SET status = 'canceled', reason = 'canceled', ended_at = ? "
                "WHERE id = ?",
                ["2026-01-01T00:00:00.000Z", JOB],
            )
            lease_release(tx, f"job:{JOB}", sleeper)

    t = threading.Thread(target=cancel_then_release)
    t.start()
    assert make_shim(store).run() == 0
    t.join()
    assert spawned == []
    row = job(store)
    assert (row["status"], row["reason"]) == ("canceled", "canceled")
    assert attempt(store)["effective"] is None and attempt(store)["running_at"] is None
    assert store.query_one("SELECT 1 FROM leases WHERE name = ?", [f"job:{JOB}"]) is None


def test_the_job_leader_is_reaped_after_the_final_commit_and_returncode_is_set(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spike C2: the leader is held unreaped (`WNOWAIT`) through the probe and the commit, and
    `Popen.returncode` is set by the manual reap so a later `wait()` does not fail."""
    from jobsctl import shim as shim_module

    seed_claimed(store, ["sh", "-c", "exit 7"])
    states: list[tuple[str, int | None]] = []
    real_finish = shim_module.finish_running
    real_reap = shim_module.reap_held_leader
    popens: list[object] = []

    def finish_spy(*args: object, **kwargs: object) -> object:
        # at the final commit the leader is still an unreaped zombie child of this process
        states.append(("at-commit", _zombie_state(popens[0].pid)))  # type: ignore[attr-defined]
        return real_finish(*args, **kwargs)  # type: ignore[arg-type]

    def reap_spy(popen: object) -> int | None:
        popens.append(popen)
        out = real_reap(popen)  # type: ignore[arg-type]
        states.append(("reaped", popen.returncode))  # type: ignore[attr-defined]
        return out

    monkeypatch.setattr(shim_module, "finish_running", finish_spy)
    # the reap comes after the commit; record the popen at spawn time instead
    from jobsctl import gateway

    real_spawn = gateway.spawn_job

    def spawn_spy(*args: object, **kwargs: object) -> object:
        popen = real_spawn(*args, **kwargs)  # type: ignore[arg-type]
        popens.append(popen)
        return popen

    monkeypatch.setattr(gateway, "spawn_job", spawn_spy)
    monkeypatch.setattr(shim_module, "reap_held_leader", reap_spy)
    make_shim(store).run()
    assert ("at-commit", ord("Z")) in states
    assert ("reaped", 7) in states
    with pytest.raises(ChildProcessError):
        os.waitpid(popens[0].pid, os.WNOHANG)  # type: ignore[attr-defined]


def _zombie_state(pid: int) -> int | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return ord(text.rsplit(")", 1)[1].split()[0])


def test_a_job_spawned_but_not_recorded_is_killed_so_no_unknown_process_runs(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If anything fails between the spawn and the commit of `running`, the transaction rolls back
    and the shim kills the group it just created (its unreaped child): no process runs that the
    database does not know."""
    from jobsctl import gateway
    from jobsctl import shim as shim_module

    seed_claimed(store, ["sleep", "60"])
    spawned: list[object] = []
    real_spawn = gateway.spawn_job

    def spawn_spy(*args: object, **kwargs: object) -> object:
        popen = real_spawn(*args, **kwargs)  # type: ignore[arg-type]
        spawned.append(popen)
        return popen

    def boom(*args: object, **kwargs: object) -> str:
        raise OSError("cannot read the identity")

    monkeypatch.setattr(gateway, "spawn_job", spawn_spy)
    monkeypatch.setattr(shim_module, "identity_text", boom)
    with pytest.raises(OSError):
        make_shim(store).run()
    (popen,) = spawned
    assert popen.returncode is not None  # type: ignore[attr-defined]  # killed and reaped
    assert popen.returncode == -signal.SIGKILL  # type: ignore[attr-defined]
    row = job(store)
    assert row["status"] == "starting" and attempt(store)["running_at"] is None
