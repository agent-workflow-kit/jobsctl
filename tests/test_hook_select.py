"""Hook selectors: queues, exclude_queues and labels against the labels at transition time."""

from __future__ import annotations

import pytest

from jobsctl.config import Config
from jobsctl.hook_select import matches, pre_start_event_id, select_hooks


def cfg(entries: list[dict[str, object]]) -> Config:
    return Config.model_validate({"hooks": {"post-fail": entries}})


def one(**kw: object):
    c = Config.model_validate({"hooks": {"post-fail": [{"cmd": "x", **kw}]}})
    return c.hooks.post_fail[0]


@pytest.mark.parametrize(
    ("kw", "queue", "labels", "expected"),
    [
        ({}, "any", [], True),
        ({"queues": ["a"]}, "a", [], True),
        ({"queues": ["a"]}, "b", [], False),
        ({"queues": []}, "a", [], False),  # an empty list selects nothing, null selects all
        ({"exclude_queues": ["a"]}, "a", [], False),
        ({"exclude_queues": ["a"]}, "b", [], True),
        ({"queues": ["a"], "exclude_queues": ["a"]}, "a", [], False),
        ({"labels": ["k=v"]}, "q", ["k=v"], True),
        ({"labels": ["k=v"]}, "q", ["k=w"], False),
        ({"labels": ["k=v"]}, "q", [], False),
        ({"labels": ["k=v", "a=b"]}, "q", ["a=b", "z=z"], False),  # AND
        ({"labels": ["k=v", "a=b"]}, "q", ["a=b", "k=v", "z=z"], True),
        ({"labels": ["k="]}, "q", ["k="], True),
        ({"labels": ["k=v"]}, "q", ["k=vv"], False),  # exact, no prefix match
    ],
)
def test_matches(kw: dict[str, object], queue: str, labels: list[str], expected: bool) -> None:
    assert matches(one(**kw), queue, labels) is expected


def test_select_keeps_configured_index_and_order() -> None:
    config = cfg(
        [
            {"cmd": "a", "queues": ["x"]},
            {"cmd": "b"},
            {"name": "c", "cmd": "c", "labels": ["k=v"]},
        ]
    )
    got = select_hooks(config, "post-fail", "y", ["k=v"])
    assert [(s.index, s.hook_id) for s in got] == [(1, "b"), (2, "c")]
    assert [s.hook for s in got] == ["post-fail", "post-fail"]


def test_select_unknown_hook_is_a_programming_error() -> None:
    with pytest.raises(KeyError):
        select_hooks(Config(), "pre-add", "q", [])


def test_pre_start_event_id() -> None:
    assert pre_start_event_id("clm-1", 2) == "clm-1:pre-start:2"
