"""Hook payloads validate against schemas/hooks/ and never carry env values."""

from __future__ import annotations

import json

import jsonschema
import pytest

from jobsctl.hook_payload import build_payload, encode_payload
from tests.conftest import validate_against
from tests.hook_fixtures import JOB_ID, TS, attempt_object, job_object


def terminal(status: str, reason: str) -> tuple[dict, dict]:
    exit_record = (
        {"kind": "exited", "code": 0, "signal": None, "core": False}
        if status == "done"
        else {"kind": "signaled", "code": None, "signal": 15, "core": False}
    )
    common = {"exit": exit_record, "leftovers": 0, "resolved_path": "/bin/echo"}
    job = job_object(status, reason=reason, ended_at=TS, **common)
    attempt = attempt_object(
        status, reason=reason, ended_at=TS, running_at=TS, effective=job["effective"], **common
    )
    return job, attempt


def pre_start() -> dict[str, object]:
    return build_payload(
        "pre-start",
        event_id="clm-1:pre-start:0",
        job=job_object("starting"),
        previous_status="queued",
        attempt=attempt_object("starting"),
    )


def test_pre_start_payload_validates() -> None:
    payload = pre_start()
    validate_against("hooks/pre-start.json", payload)
    assert payload["repo"] is None and payload["object_id"] == JOB_ID
    assert payload["tool"] == "jobsctl" and payload["object_type"] == "job"
    assert "output_tail" not in payload and "output_available" not in payload


def test_env_values_never_reach_the_payload_only_sorted_names() -> None:
    payload = pre_start()
    job = payload["job"]
    assert isinstance(job, dict)
    assert "env" not in job and job["env_names"] == ["A", "B"]
    assert '"2"' not in json.dumps(payload).replace('"B"', "")


@pytest.mark.parametrize("hook", ["post-start"])
def test_post_start_has_no_output_fields(hook: str) -> None:
    payload = build_payload(
        hook,
        event_id="e1",
        job=job_object("running", resolved_path="/bin/echo"),
        previous_status="starting",
        attempt=attempt_object(
            "running",
            running_at=TS,
            resolved_path="/bin/echo",
            effective={"timeout": None, "max_retries": 0, "kill_grace": 30},
        ),
    )
    validate_against(f"hooks/{hook}.json", payload)
    assert "output_available" not in payload


@pytest.mark.parametrize(
    ("hook", "status", "reason"),
    [("post-finish", "done", "exit"), ("post-fail", "failed", "timeout")],
)
def test_output_hooks_include_the_tail_only_when_asked(
    hook: str, status: str, reason: str
) -> None:
    job, attempt = terminal(status, reason)
    kw = {"event_id": "e1", "job": job, "previous_status": "running", "attempt": attempt}
    with_tail = build_payload(
        hook, output_available=True, output_tail="tail", include_output=True, **kw
    )
    validate_against(f"hooks/{hook}.json", with_tail)
    assert with_tail["output_tail"] == "tail" and with_tail["output_available"] is True
    without = build_payload(hook, output_available=True, output_tail="tail", **kw)
    validate_against(f"hooks/{hook}.json", without)
    assert without["output_tail"] is None and without["output_available"] is True
    none_ran = build_payload(hook, output_available=False, output_tail="x", include_output=True, **kw)
    validate_against(f"hooks/{hook}.json", none_ran)
    assert none_ran["output_tail"] is None and none_ran["output_available"] is False


def test_post_cancel_of_a_queued_job_has_a_null_attempt() -> None:
    payload = build_payload(
        "post-cancel",
        event_id="e9",
        job=job_object("canceled", reason="canceled", ended_at=TS, attempt=1, claimed_at=None),
        previous_status="queued",
        attempt=None,
    )
    validate_against("hooks/post-cancel.json", payload)
    assert payload["attempt"] is None and payload["output_available"] is False


def test_post_held_validates() -> None:
    payload = build_payload(
        "post-held",
        event_id="e2",
        job=job_object("held", hold_reason="manual", claimed_at=None),
        previous_status="queued",
        attempt=None,
    )
    validate_against("hooks/post-held.json", payload)


def test_a_payload_with_an_extra_field_is_rejected_by_the_schema() -> None:
    payload = pre_start()
    payload["extra"] = 1
    with pytest.raises(jsonschema.ValidationError):
        validate_against("hooks/pre-start.json", payload)


def test_output_tail_is_capped() -> None:
    job, attempt = terminal("failed", "timeout")
    payload = build_payload(
        "post-fail",
        event_id="e",
        job=job,
        previous_status="running",
        attempt=attempt,
        output_available=True,
        output_tail="x" * 300000,
        include_output=True,
    )
    assert len(payload["output_tail"]) == 262144  # type: ignore[arg-type]
    validate_against("hooks/post-fail.json", payload)


def test_encoding_is_ascii_json_with_newline_even_for_lone_surrogates() -> None:
    payload = pre_start()
    payload["labels"] = ["k=\ud800", "k=é "]
    raw = encode_payload(payload)
    assert raw.isascii() and raw.endswith(b"\n") and raw.count(b"\n") == 1
    assert json.loads(raw)["labels"] == ["k=\ud800", "k=é "]
