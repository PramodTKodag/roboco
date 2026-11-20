"""Wire protocol between trusted callers and the fixed-profile launcher.

One newline-terminated JSON frame per request. Every body is a frozen model with
``extra="forbid"``, so a frame can never smuggle an image, argv, env, mount,
network, device or capability: the server re-validates regardless of what the
client did.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID  # noqa: TC003 - pydantic resolves annotations at runtime

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from roboco.runtime.subscription.contracts import (
    MAX_FILE_BYTES,
    WorkerJob,
    WorkerResult,
)

MAX_FRAME_BYTES = 2 * MAX_FILE_BYTES + 64 * 1024
MAX_RESPONSE_BYTES = 256 * 1024


class ProtocolError(ValueError):
    """Malformed, oversized or unauthorized-shape frame."""


class LauncherAction(StrEnum):
    HEALTH = "health"
    REGISTER_WORKSPACE = "register_workspace"
    REGISTER_RUN = "register_run"
    CANCEL_RUN = "cancel_run"
    EXECUTE = "execute"


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class HealthBody(_Body):
    pass


class RegisterWorkspaceBody(_Body):
    workspace_handle: UUID
    project_id: UUID


class RegisterRunBody(_Body):
    run_id: UUID
    project_id: UUID
    workspace_handle: UUID


class CancelRunBody(_Body):
    run_id: UUID


class ExecuteBody(_Body):
    job: WorkerJob


class _Frame(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    token: Annotated[str, Field(min_length=1, max_length=256)]


class HealthFrame(_Frame):
    action: Literal[LauncherAction.HEALTH]
    body: HealthBody


class RegisterWorkspaceFrame(_Frame):
    action: Literal[LauncherAction.REGISTER_WORKSPACE]
    body: RegisterWorkspaceBody


class RegisterRunFrame(_Frame):
    action: Literal[LauncherAction.REGISTER_RUN]
    body: RegisterRunBody


class CancelRunFrame(_Frame):
    action: Literal[LauncherAction.CANCEL_RUN]
    body: CancelRunBody


class ExecuteFrame(_Frame):
    action: Literal[LauncherAction.EXECUTE]
    body: ExecuteBody


LauncherFrame = Annotated[
    HealthFrame
    | RegisterWorkspaceFrame
    | RegisterRunFrame
    | CancelRunFrame
    | ExecuteFrame,
    Field(discriminator="action"),
]
_FRAME_ADAPTER: TypeAdapter[LauncherFrame] = TypeAdapter(LauncherFrame)


def parse_frame(raw: bytes) -> LauncherFrame:
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


class LauncherResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ok: bool
    reason_code: str | None = None
    result: WorkerResult | None = None


def encode_response(response: LauncherResponse) -> bytes:
    return response.model_dump_json().encode() + b"\n"
