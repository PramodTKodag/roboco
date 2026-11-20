"""Wire protocol between the controller and the Claude service.

One newline-terminated JSON frame per request, validated by frozen models with
``extra="forbid"`` on both ends. The three actions carry run authority only:
no frame can name an executable, argument, environment, mount, URL, provider or
credential, and there is no generic completion operation.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID  # noqa: TC003 - pydantic resolves annotations at runtime

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from roboco.runtime.subscription.contracts import (
    MAX_PROMPT_BYTES,
    RunRequest,
    RunResult,
    SubscriptionStatus,
)

MAX_FRAME_BYTES = 2 * MAX_PROMPT_BYTES
MAX_RESPONSE_BYTES = MAX_PROMPT_BYTES


class ProtocolError(ValueError):
    """Malformed, oversized or unauthorized-shape frame."""


class ServiceAction(StrEnum):
    STATUS = "status"
    RUN = "run"
    CANCEL = "cancel"


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StatusBody(_Body):
    pass


class CancelBody(_Body):
    run_id: UUID


class _Frame(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    token: Annotated[str, Field(min_length=1, max_length=256)]


class StatusFrame(_Frame):
    action: Literal[ServiceAction.STATUS]
    body: StatusBody


class RunFrame(_Frame):
    action: Literal[ServiceAction.RUN]
    body: RunRequest


class CancelFrame(_Frame):
    action: Literal[ServiceAction.CANCEL]
    body: CancelBody


ServiceFrame = Annotated[
    StatusFrame | RunFrame | CancelFrame, Field(discriminator="action")
]
_FRAME_ADAPTER: TypeAdapter[ServiceFrame] = TypeAdapter(ServiceFrame)


def parse_frame(raw: bytes) -> ServiceFrame:
    """Validate one request frame; reasons never echo the frame's contents."""
    if len(raw) > MAX_FRAME_BYTES:
        raise ProtocolError("frame_too_large")
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProtocolError("frame_not_json") from exc
    if not isinstance(decoded, dict):
        raise ProtocolError("frame_not_object")
    try:
        return _FRAME_ADAPTER.validate_python(decoded)
    except ValidationError as exc:
        raise ProtocolError("frame_invalid") from exc


class ServiceResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ok: bool
    reason_code: str | None = None
    status: SubscriptionStatus | None = None
    result: RunResult | None = None
    cancelled: bool | None = None


def encode_response(response: ServiceResponse) -> bytes:
    return response.model_dump_json().encode() + b"\n"
