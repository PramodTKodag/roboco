"""Frozen, bounded contracts for subscription runs.

No field carries environment, argv, URL, mount or credential material: those
are fixed by trusted setup and can never arrive from a task or the model.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - pydantic resolves annotations at runtime
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID  # noqa: TC003 - pydantic resolves annotations at runtime

from pydantic import BaseModel, ConfigDict, StrictInt, field_validator, model_validator

from roboco.foundation.policy.subscription_policy import ALLOWED_MODEL_IDS

MAX_PROMPT_BYTES = 1024 * 1024


class SubscriptionState(StrEnum):
    READY = "ready"
    LOGIN_REQUIRED = "login_required"
    QUOTA_EXHAUSTED = "quota_exhausted"
    POLICY_BLOCKED = "policy_blocked"
    CONFIGURATION_ERROR = "configuration_error"


class RunState(StrEnum):
    COMPLETED = "completed"
    LOGIN_REQUIRED = "login_required"
    QUOTA_EXHAUSTED = "quota_exhausted"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SubscriptionStatus(_Frozen):
    """Redacted account status: no email, raw auth JSON, token or credential path."""

    state: SubscriptionState
    authenticated: bool
    auth_method: str | None
    checked_at: datetime
    reason_code: str | None

    @model_validator(mode="after")
    def _ready_implies_authenticated(self) -> Self:
        if self.state is SubscriptionState.READY and not self.authenticated:
            raise ValueError("a ready subscription must be authenticated")
        return self


class RunSpec(_Frozen):
    run_id: UUID
    project_id: UUID | None = None
    task_id: UUID | None = None
    agent_id: UUID | None = None
    purpose: Literal["delivery", "intake", "secretary", "background"]
    model: str
    prompt: str
    workspace_handle: UUID | None = None

    @field_validator("model")
    @classmethod
    def _model_is_an_allowed_exact_id(cls, value: str) -> str:
        if value not in ALLOWED_MODEL_IDS:
            raise ValueError("model is not an allowed exact Claude model id")
        return value

    @field_validator("prompt")
    @classmethod
    def _prompt_is_bounded_text(cls, value: str) -> str:
        if not value:
            raise ValueError("prompt must not be empty")
        if "\x00" in value:
            raise ValueError("prompt must not contain NUL")
        if len(value.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise ValueError("prompt exceeds the 1 MiB limit")
        return value


class RunResult(_Frozen):
    """Terminal outcome. Partial text under a non-completed state is incomplete."""

    run_id: UUID
    state: RunState
    text: str
    reason_code: str | None
    usage: dict[str, StrictInt]
