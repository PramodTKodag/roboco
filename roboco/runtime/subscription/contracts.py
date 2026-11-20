"""Frozen, bounded contracts for subscription runs.

No field carries environment, argv, URL, mount or credential material: those
are fixed by trusted setup and can never arrive from a task or the model.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - pydantic resolves annotations at runtime
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID  # noqa: TC003 - pydantic resolves annotations at runtime

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from roboco.foundation.policy.subscription_policy import ALLOWED_MODEL_IDS

MAX_PROMPT_BYTES = 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024
MAX_STREAM_BYTES = 64 * 1024
MAX_COMMAND_BYTES = 64 * 1024
MAX_PATH_BYTES = 1024
COMMAND_TIMEOUT_SECONDS = 600


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


# ---------------------------------------------------------------------------
# Worker jobs: typed requests for the disposable offline worker. They carry
# relative paths and content only; image, argv, env, mounts, network, limits and
# capabilities are fixed by the launcher's profile and can never be requested.
# ---------------------------------------------------------------------------


def _check_relative_path(value: str) -> str:
    if not value or len(value.encode("utf-8")) > MAX_PATH_BYTES:
        raise ValueError("path must be 1..1024 bytes")
    if "\x00" in value or "\\" in value:
        raise ValueError("path contains a forbidden character")
    if value.startswith(("/", "~")):
        raise ValueError("path must be relative to the workspace")
    segments = value.split("/")
    if any(segment in {"", ".."} for segment in segments):
        raise ValueError("path must not contain empty or '..' segments")
    return value


RelativePath = Annotated[str, AfterValidator(_check_relative_path)]


def _bounded_text(limit: int, what: str) -> AfterValidator:
    def check(value: str) -> str:
        if len(value.encode("utf-8")) > limit:
            raise ValueError(f"{what} exceeds {limit} bytes")
        return value

    return AfterValidator(check)


class WorkerOperation(StrEnum):
    READ_FILE = "read_file"
    LIST_FILES = "list_files"
    SEARCH_TEXT = "search_text"
    WRITE_FILE = "write_file"
    APPLY_PATCH = "apply_patch"
    RUN_COMMAND = "run_command"
    EXPORT_PATCH = "export_patch"
    CHECK_CONVENTIONS = "check_conventions"


class ReadFilePayload(_Frozen):
    operation: Literal["read_file"] = "read_file"
    path: RelativePath
    offset: Annotated[int, Field(ge=0, le=MAX_FILE_BYTES)] = 0


class ListFilesPayload(_Frozen):
    operation: Literal["list_files"] = "list_files"
    path: RelativePath = "."


class SearchTextPayload(_Frozen):
    operation: Literal["search_text"] = "search_text"
    pattern: Annotated[str, Field(min_length=1), _bounded_text(1024, "pattern")]
    path: RelativePath = "."


class WriteFilePayload(_Frozen):
    operation: Literal["write_file"] = "write_file"
    path: RelativePath
    content: Annotated[str, _bounded_text(MAX_FILE_BYTES, "content")]


class ApplyPatchPayload(_Frozen):
    operation: Literal["apply_patch"] = "apply_patch"
    patch: Annotated[str, Field(min_length=1), _bounded_text(MAX_FILE_BYTES, "patch")]


class RunCommandPayload(_Frozen):
    """Shell text runs only inside the offline worker, never in a trusted part."""

    operation: Literal["run_command"] = "run_command"
    command: Annotated[
        str, Field(min_length=1), _bounded_text(MAX_COMMAND_BYTES, "command")
    ]


class ExportPatchPayload(_Frozen):
    operation: Literal["export_patch"] = "export_patch"


MAX_CONVENTION_FILES = 500


class CheckConventionsPayload(_Frozen):
    """Validate named files against the project's architectural conventions."""

    operation: Literal["check_conventions"] = "check_conventions"
    files: Annotated[list[RelativePath], Field(max_length=MAX_CONVENTION_FILES)]


WorkerPayload = Annotated[
    ReadFilePayload
    | ListFilesPayload
    | SearchTextPayload
    | WriteFilePayload
    | ApplyPatchPayload
    | RunCommandPayload
    | ExportPatchPayload
    | CheckConventionsPayload,
    Field(discriminator="operation"),
]


class WorkerJob(_Frozen):
    job_id: UUID
    run_id: UUID
    project_id: UUID
    workspace_handle: UUID
    operation: WorkerOperation
    payload: WorkerPayload

    @model_validator(mode="after")
    def _payload_matches_operation(self) -> Self:
        if self.payload.operation != self.operation.value:
            raise ValueError("payload does not match the declared operation")
        return self


class WorkerResult(_Frozen):
    job_id: UUID
    exit_code: int | None
    stdout: Annotated[str, _bounded_text(MAX_STREAM_BYTES, "stdout")]
    stderr: Annotated[str, _bounded_text(MAX_STREAM_BYTES, "stderr")]
    truncated: bool
    reason_code: str | None


# ---------------------------------------------------------------------------
# Workspace import: an owner-operated snapshot archive admitted inside the
# offline worker. Only the options below can accompany the archive bytes.
# ---------------------------------------------------------------------------

MAX_ARCHIVE_BYTES = 300 * 1024 * 1024
MAX_IMPORT_LIST_ITEMS = 256
MAX_IMPORT_REPORT_ITEMS = 100
IMPORT_TIMEOUT_SECONDS = 900
IMPORT_MODE_ARGUMENT = "import"

_ImportListItem = Annotated[str, Field(min_length=1, max_length=1024)]


class ImportOptions(_Frozen):
    """Owner choices for an import: extra deny globs and acknowledged findings."""

    extra_deny_globs: Annotated[
        tuple[_ImportListItem, ...], Field(max_length=MAX_IMPORT_LIST_ITEMS)
    ] = ()
    allowed_findings: Annotated[
        tuple[_ImportListItem, ...], Field(max_length=MAX_IMPORT_LIST_ITEMS)
    ] = ()


class ImportFinding(_Frozen):
    """A suspected secret by location and rule; never the matched text."""

    path: str
    rule: str


class ImportOutcome(_Frozen):
    ok: bool
    reason_code: str | None = None
    files: StrictInt = 0
    directories: StrictInt = 0
    total_bytes: StrictInt = 0
    excluded: Annotated[tuple[str, ...], Field(max_length=MAX_IMPORT_REPORT_ITEMS)] = ()
    findings: Annotated[
        tuple[ImportFinding, ...], Field(max_length=MAX_IMPORT_REPORT_ITEMS)
    ] = ()
