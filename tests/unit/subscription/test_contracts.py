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
