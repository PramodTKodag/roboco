"""Frozen, bounded run contracts: no free-form env, argv, URL or mount fields."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from roboco.foundation.policy.subscription_policy import ALLOWED_MODEL_IDS
from roboco.runtime.subscription.contracts import (
    RunResult,
    RunSpec,
    RunState,
    SubscriptionState,
    SubscriptionStatus,
)

MODEL = sorted(ALLOWED_MODEL_IDS)[0]
INPUT_TOKENS = 3


def _spec(**overrides: object) -> RunSpec:
    base: dict[str, object] = {
        "run_id": uuid4(),
        "purpose": "delivery",
        "model": MODEL,
        "prompt": "hello",
    }
    return RunSpec(**{**base, **overrides})  # type: ignore[arg-type]


def test_minimal_spec_is_valid_and_frozen() -> None:
    spec = _spec()
    with pytest.raises(ValidationError):
        spec.prompt = "changed"  # type: ignore[misc]


@pytest.mark.parametrize("field", ["env", "argv", "url", "mounts", "cwd"])
def test_free_form_fields_are_forbidden(field: str) -> None:
    with pytest.raises(ValidationError):
        _spec(**{field: "x"})


@pytest.mark.parametrize("model", ["sonnet", "opus", "latest", "gpt-4o", "", "claude"])
def test_aliases_and_unknown_models_are_rejected(model: str) -> None:
    with pytest.raises(ValidationError):
        _spec(model=model)


def test_unknown_purpose_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _spec(purpose="admin")


def test_prompt_limit_counts_utf8_bytes() -> None:
    _spec(prompt="a" * (1024 * 1024))
    with pytest.raises(ValidationError):
        _spec(prompt="a" * (1024 * 1024 + 1))
    with pytest.raises(ValidationError):
        _spec(prompt="é" * (512 * 1024 + 1))


def test_prompt_must_not_be_empty_or_contain_nul() -> None:
    with pytest.raises(ValidationError):
        _spec(prompt="")
    with pytest.raises(ValidationError):
        _spec(prompt="a\x00b")


def test_status_has_no_secret_bearing_fields() -> None:
    status = SubscriptionStatus(
        state=SubscriptionState.READY,
        authenticated=True,
        auth_method="claude.ai",
        checked_at=datetime.now(UTC),
        reason_code=None,
    )
    assert set(status.model_dump()) == {
        "state",
        "authenticated",
        "auth_method",
        "checked_at",
        "reason_code",
    }
    with pytest.raises(ValidationError):
        SubscriptionStatus(
            state=SubscriptionState.READY,
            authenticated=True,
            auth_method="claude.ai",
            checked_at=datetime.now(UTC),
            reason_code=None,
            email="owner@example.test",  # type: ignore[call-arg]
        )


def test_ready_status_must_be_authenticated() -> None:
    with pytest.raises(ValidationError):
        SubscriptionStatus(
            state=SubscriptionState.READY,
            authenticated=False,
            auth_method=None,
            checked_at=datetime.now(UTC),
            reason_code=None,
        )


def test_result_usage_is_integer_counters_only() -> None:
    result = RunResult(
        run_id=uuid4(),
        state=RunState.COMPLETED,
        text="ok",
        reason_code=None,
        usage={"input_tokens": INPUT_TOKENS, "output_tokens": 1},
    )
    assert result.usage["input_tokens"] == INPUT_TOKENS
    with pytest.raises(ValidationError):
        RunResult(
            run_id=uuid4(),
            state=RunState.COMPLETED,
            text="ok",
            reason_code=None,
            usage={"cost_usd": 0.5},  # type: ignore[dict-item]
        )


# ---------------------------------------------------------------------------
# Worker contracts
# ---------------------------------------------------------------------------

from roboco.runtime.subscription.contracts import (  # noqa: E402
    MAX_STREAM_BYTES,
    WorkerJob,
    WorkerOperation,
    WorkerResult,
)


def _job(operation: str, payload: dict[str, object], **overrides: object) -> WorkerJob:
    base: dict[str, object] = {
        "job_id": uuid4(),
        "run_id": uuid4(),
        "project_id": uuid4(),
        "workspace_handle": uuid4(),
        "operation": operation,
        "payload": {"operation": operation, **payload},
    }
    return WorkerJob(**{**base, **overrides})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("operation", "payload"),
    [
        ("read_file", {"path": "src/app.py"}),
        ("list_files", {"path": "."}),
        ("search_text", {"pattern": "TODO", "path": "src"}),
        ("write_file", {"path": "a.txt", "content": "x"}),
        ("apply_patch", {"patch": "--- a\n+++ b\n"}),
        ("run_command", {"command": "pytest -q"}),
        ("export_patch", {}),
    ],
)
def test_every_operation_has_a_valid_payload(
    operation: str, payload: dict[str, object]
) -> None:
    assert _job(operation, payload).operation is WorkerOperation(operation)


def test_payload_must_match_the_declared_operation() -> None:
    with pytest.raises(ValidationError):
        WorkerJob(
            job_id=uuid4(),
            run_id=uuid4(),
            project_id=uuid4(),
            workspace_handle=uuid4(),
            operation=WorkerOperation.READ_FILE,
            payload={"operation": "run_command", "command": "id"},  # type: ignore[arg-type]
        )


def test_unknown_operation_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _job("docker_run", {"image": "evil"})


@pytest.mark.parametrize(
    "field", ["image", "argv", "env", "mounts", "network", "capabilities", "timeout"]
)
def test_job_payloads_reject_launch_shaping_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        _job("run_command", {"command": "id", field: "x"})
    with pytest.raises(ValidationError):
        _job("read_file", {"path": "a", field: "x"})


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../x", "a/../../x", "a/./b/../../../c", "a\x00b", "", "a\\b", "~"],
)
def test_paths_must_be_safe_and_relative(path: str) -> None:
    with pytest.raises(ValidationError):
        _job("read_file", {"path": path})


def test_oversized_content_and_command_are_rejected() -> None:
    with pytest.raises(ValidationError):
        _job("write_file", {"path": "a", "content": "x" * (1024 * 1024 + 1)})
    with pytest.raises(ValidationError):
        _job("run_command", {"command": "x" * (64 * 1024 + 1)})


def test_worker_result_is_bounded_per_stream() -> None:
    WorkerResult(
        job_id=uuid4(),
        exit_code=0,
        stdout="x" * MAX_STREAM_BYTES,
        stderr="",
        truncated=False,
        reason_code=None,
    )
    with pytest.raises(ValidationError):
        WorkerResult(
            job_id=uuid4(),
            exit_code=0,
            stdout="x" * (MAX_STREAM_BYTES + 1),
            stderr="",
            truncated=False,
            reason_code=None,
        )
