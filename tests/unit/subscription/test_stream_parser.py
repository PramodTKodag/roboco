"""Stream parsing: exact tool surface, terminal classification, bounded output."""

from __future__ import annotations

import json
from typing import Any

import pytest
from roboco.runtime.subscription.contracts import MAX_RUN_TURNS, RunState
from roboco.runtime.subscription.stream_parser import (
    MAX_RESULT_TEXT_BYTES,
    StreamParser,
)

TOOLS = frozenset({"mcp__roboco_worker__read_file"})


def _line(event: dict[str, Any]) -> bytes:
    return json.dumps(event).encode() + b"\n"


def _init(tools: list[str] | None = None) -> bytes:
    names = sorted(TOOLS) if tools is None else tools
    return _line({"type": "system", "subtype": "init", "tools": names})


def _assistant(text: str = "working", error: str | None = None) -> bytes:
    event: dict[str, Any] = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }
    if error is not None:
        event["error"] = error
    return _line(event)


def _result(**overrides: Any) -> bytes:
    event: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "final answer",
        "usage": {"input_tokens": 5, "output_tokens": 7, "service_tier": "standard"},
    }
    event.update(overrides)
    return _line(event)


def _parse(*lines: bytes, tools: frozenset[str] = TOOLS) -> StreamParser:
    parser = StreamParser(expected_tools=tools)
    for line in lines:
        parser.feed(line)
    return parser


def test_clean_run_completes_with_filtered_usage() -> None:
    outcome = _parse(_init(), _assistant(), _result()).outcome()
    assert outcome.state is RunState.COMPLETED
    assert outcome.reason_code is None
    assert outcome.text == "final answer"
    assert outcome.usage == {"input_tokens": 5, "output_tokens": 7}


@pytest.mark.parametrize(
    "advertised",
    [
        ["Bash"],
        sorted([*TOOLS, "Read"]),
        ["mcp__roboco_worker__read_file_extra"],
        ["mcp__roboco_worker"],
        [],
    ],
)
def test_any_difference_in_the_tool_surface_stops_the_run(
    advertised: list[str],
) -> None:
    parser = StreamParser(expected_tools=TOOLS)
    assert parser.feed(_init(advertised)) == "tool_surface_mismatch"
    outcome = parser.outcome()
    assert outcome.state is RunState.BLOCKED
    assert outcome.reason_code == "tool_surface_mismatch"


def test_a_tool_free_run_must_advertise_no_tools() -> None:
    assert StreamParser(frozenset()).feed(_init([])) is None
    assert StreamParser(frozenset()).feed(_init(["Bash"])) == "tool_surface_mismatch"


def test_malformed_init_tools_are_a_mismatch() -> None:
    event = _line({"type": "system", "subtype": "init", "tools": "Bash"})
    assert StreamParser(TOOLS).feed(event) == "tool_surface_mismatch"


def test_model_output_before_init_is_a_violation() -> None:
    assert StreamParser(TOOLS).feed(_assistant()) == "init_event_missing"


def test_exceeding_the_turn_limit_stops_the_run() -> None:
    parser = StreamParser(TOOLS)
    parser.feed(_init())
    reasons = [parser.feed(_assistant()) for _ in range(MAX_RUN_TURNS + 1)]
    assert reasons[:MAX_RUN_TURNS] == [None] * MAX_RUN_TURNS
    assert reasons[-1] == "turn_limit"
    assert parser.outcome().state is RunState.BLOCKED


def test_vendor_max_turns_result_is_blocked_not_completed() -> None:
    outcome = _parse(
        _init(),
        _assistant("partial"),
        _result(subtype="error_max_turns", is_error=True),
    ).outcome()
    assert outcome.state is RunState.BLOCKED
    assert outcome.reason_code == "turn_limit"
    assert outcome.text == "partial"


@pytest.mark.parametrize(
    "assistant_error,state,reason",
    [
        ("authentication_failed", RunState.LOGIN_REQUIRED, "login_required"),
        ("rate_limit", RunState.QUOTA_EXHAUSTED, "quota_exhausted"),
        ("billing_error", RunState.QUOTA_EXHAUSTED, "quota_exhausted"),
        ("server_error", RunState.FAILED, "model_run_failed"),
        ("invalid_request", RunState.FAILED, "model_run_failed"),
    ],
)
def test_error_results_map_to_explicit_states(
    assistant_error: str, state: RunState, reason: str
) -> None:
    outcome = _parse(
        _init(),
        _assistant("oops", error=assistant_error),
        _result(is_error=True, result="vendor failure text"),
    ).outcome()
    assert outcome.state is state
    assert outcome.reason_code == reason
    assert outcome.text == ""


def test_not_logged_in_text_in_an_error_result_requires_login() -> None:
    outcome = _parse(
        _init(), _result(is_error=True, result="Not logged in · Please run /login")
    ).outcome()
    assert outcome.state is RunState.LOGIN_REQUIRED


def test_login_text_in_a_successful_result_is_just_text() -> None:
    outcome = _parse(
        _init(), _result(result="Please run /login in your shell")
    ).outcome()
    assert outcome.state is RunState.COMPLETED


def test_missing_terminal_event_is_a_failure_with_partial_output() -> None:
    outcome = _parse(_init(), _assistant("half done")).outcome()
    assert outcome.state is RunState.FAILED
    assert outcome.reason_code == "no_terminal_event"
    assert outcome.text == "half done"


def test_garbage_and_non_object_lines_are_ignored() -> None:
    outcome = _parse(
        b"not json\n", b"[1,2]\n", b'"str"\n', _init(), b"\xff\xfe\n", _result()
    ).outcome()
    assert outcome.state is RunState.COMPLETED


def test_result_text_is_bounded_on_a_character_boundary() -> None:
    huge = "é" * MAX_RESULT_TEXT_BYTES
    outcome = _parse(_init(), _result(result=huge)).outcome()
    assert len(outcome.text.encode()) <= MAX_RESULT_TEXT_BYTES
    assert set(outcome.text) == {"é"}


def test_partial_outcome_for_an_external_stop_keeps_bounded_partial_text() -> None:
    parser = _parse(_init(), _assistant("so far"))
    outcome = parser.partial_outcome(RunState.CANCELLED, "cancelled")
    assert (outcome.state, outcome.reason_code, outcome.text) == (
        RunState.CANCELLED,
        "cancelled",
        "so far",
    )


def test_boolean_usage_values_are_not_counted_as_integers() -> None:
    outcome = _parse(_init(), _result(usage={"input_tokens": True})).outcome()
    assert outcome.usage == {}
