"""Reduce the official CLI's stream-json output to a bounded run outcome.

Only fields this module understands are read; raw events are never stored or
logged. The advertised tool list in the ``init`` event is compared for exact
equality with the run's granted tools, so a CLI change or configuration drift
that exposes anything else stops the run before the model can use it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from roboco.runtime.subscription.contracts import (
    MAX_RUN_TURNS,
    MAX_STREAM_BYTES,
    RunState,
)

MAX_RESULT_TEXT_BYTES = 4 * MAX_STREAM_BYTES
_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)
_AUTH_ERRORS = frozenset({"authentication_failed"})
_QUOTA_ERRORS = frozenset({"rate_limit", "billing_error"})
_NOT_LOGGED_IN_MARKER = "Please run /login"


@dataclass(frozen=True)
class Outcome:
    state: RunState
    reason_code: str | None
    text: str
    usage: dict[str, int]


@dataclass
class StreamParser:
    expected_tools: frozenset[str]
    _init_seen: bool = False
    _turns: int = 0
    _partial: str = ""
    _result: dict[str, Any] | None = None
    _assistant_error: str | None = None
    _usage: dict[str, int] = field(default_factory=dict)
    _violation: str | None = None

    def feed(self, line: bytes) -> str | None:
        """Consume one output line; return a reason code if the run must stop now."""
        event = _decode(line)
        if event is None or self._violation is not None:
            return self._violation
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            self._on_init(event)
        elif kind == "assistant":
            self._on_assistant(event)
        elif kind == "result":
            self._result = event
        return self._violation

    def outcome(self) -> Outcome:
        """The terminal classification once the process has exited normally."""
        if self._violation is not None:
            return self._blocked(self._violation)
        if self._result is None:
            return self._failed("no_terminal_event")
        if self._result.get("subtype") == "error_max_turns":
            return self._blocked("turn_limit")
        if not self._result.get("is_error"):
            return Outcome(
                RunState.COMPLETED,
                None,
                _bounded(str(self._result.get("result", ""))),
                self._result_usage(),
            )
        return self._classify_error()

    def partial_outcome(self, state: RunState, reason_code: str) -> Outcome:
        """Outcome for a run ended from outside: cancelled, timed out, killed."""
        return Outcome(state, reason_code, self._partial, dict(self._usage))

    def _on_init(self, event: dict[str, Any]) -> None:
        tools = event.get("tools")
        advertised = (
            frozenset(tools)
            if isinstance(tools, list) and all(isinstance(t, str) for t in tools)
            else None
        )
        self._init_seen = True
        if advertised != self.expected_tools:
            self._violation = "tool_surface_mismatch"

    def _on_assistant(self, event: dict[str, Any]) -> None:
        if not self._init_seen:
            self._violation = "init_event_missing"
            return
        self._turns += 1
        if self._turns > MAX_RUN_TURNS:
            self._violation = "turn_limit"
            return
        error = event.get("error")
        if isinstance(error, str):
            self._assistant_error = error
        message = event.get("message")
        if not isinstance(message, dict):
            return
        text = "".join(
            block.get("text", "")
            for block in message.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if text:
            self._partial = _bounded(text)

    def _result_usage(self) -> dict[str, int]:
        usage = self._result.get("usage") if self._result else None
        if not isinstance(usage, dict):
            return {}
        return {
            key: value
            for key in _USAGE_KEYS
            if isinstance(value := usage.get(key), int) and not isinstance(value, bool)
        }

    def _classify_error(self) -> Outcome:
        text = str(self._result.get("result", "")) if self._result else ""
        if self._assistant_error in _AUTH_ERRORS or _NOT_LOGGED_IN_MARKER in text:
            return Outcome(
                RunState.LOGIN_REQUIRED, "login_required", "", self._result_usage()
            )
        if self._assistant_error in _QUOTA_ERRORS:
            return Outcome(
                RunState.QUOTA_EXHAUSTED, "quota_exhausted", "", self._result_usage()
            )
        return Outcome(RunState.FAILED, "model_run_failed", "", self._result_usage())

    def _blocked(self, reason_code: str) -> Outcome:
        return Outcome(RunState.BLOCKED, reason_code, self._partial, dict(self._usage))

    def _failed(self, reason_code: str) -> Outcome:
        return Outcome(RunState.FAILED, reason_code, self._partial, dict(self._usage))


def _decode(line: bytes) -> dict[str, Any] | None:
    try:
        event = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    return event if isinstance(event, dict) else None


def _bounded(text: str) -> str:
    data = text.encode("utf-8")
    if len(data) <= MAX_RESULT_TEXT_BYTES:
        return text
    return data[:MAX_RESULT_TEXT_BYTES].decode("utf-8", errors="ignore")
