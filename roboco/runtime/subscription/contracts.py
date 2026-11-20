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
    StringConstraints,
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
# Trusted per-run limits. Raising them is an owner configuration change, never
# a task or model argument.
MAX_RUN_SECONDS = 1800
MAX_RUN_TURNS = 32
MAX_QUEUED_RUNS = 64
# The CLI's own default is 10 retries; the plan allows two on one configuration.
CLI_MAX_RETRIES = 2


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


class WorkerOperation(StrEnum):
    READ_FILE = "read_file"
    LIST_FILES = "list_files"
    SEARCH_TEXT = "search_text"
    WRITE_FILE = "write_file"
    APPLY_PATCH = "apply_patch"
    RUN_COMMAND = "run_command"
    EXPORT_PATCH = "export_patch"
    CHECK_CONVENTIONS = "check_conventions"


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


class VerbServer(StrEnum):
    """The trusted MCP servers that expose business verbs to a run.

    The module behind each name is fixed in the service, so a controller can grant
    verbs but never choose what code the CLI launches.
    """

    FLOW = "flow"
    DO = "do"


_VERB_NAME = r"^[a-z][a-z0-9_]{0,63}$"
_SERVERS_ALLOWED_FOR_PURPOSE: dict[str, frozenset[VerbServer]] = {
    "delivery": frozenset({VerbServer.FLOW, VerbServer.DO}),
    "intake": frozenset(),
    "secretary": frozenset(),
    "background": frozenset(),
}


class VerbGrant(_Frozen):
    """The exact verbs one trusted MCP server may expose to a run."""

    server: VerbServer
    verbs: tuple[Annotated[str, StringConstraints(pattern=_VERB_NAME)], ...]

    @field_validator("verbs")
    @classmethod
    def _verbs_are_unique_and_present(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(set(value)) != len(value):
            raise ValueError("a verb grant needs unique, non-empty verbs")
        return value


class ToolManifest(_Frozen):
    """What one run may use: worker operations and business verbs.

    Empty means a tool-free run.
    """

    operations: tuple[WorkerOperation, ...] = ()
    verb_grants: tuple[VerbGrant, ...] = ()

    @field_validator("operations")
    @classmethod
    def _operations_are_unique(
        cls, value: tuple[WorkerOperation, ...]
    ) -> tuple[WorkerOperation, ...]:
        if len(set(value)) != len(value):
            raise ValueError("manifest operations must be unique")
        return value

    @field_validator("verb_grants")
    @classmethod
    def _one_grant_per_server(
        cls, value: tuple[VerbGrant, ...]
    ) -> tuple[VerbGrant, ...]:
        servers = [grant.server for grant in value]
        if len(set(servers)) != len(servers):
            raise ValueError("a server may appear in only one verb grant")
        return value


class VerbCredentials(_Frozen):
    """The orchestrator-minted, short-lived identity a run's verb servers use.

    Held only by the trusted service and handed to the verb servers' environment,
    never to the model. The token is bound by the orchestrator to this agent and
    run; the service does not interpret it.
    """

    agent_id: UUID
    role: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,40}$")]
    token: Annotated[str, StringConstraints(min_length=16, max_length=2048)] = Field(
        repr=False
    )


class RunRequest(_Frozen):
    """A run plus the tool manifest the controller grants it.

    A delivery run without a manifest is refused by the service: it never falls
    back to a default tool set. Verb grants need verb credentials, and the servers
    a purpose may use are fixed by ``servers_allowed_for``.
    """

    spec: RunSpec
    manifest: ToolManifest | None = None
    verb_credentials: VerbCredentials | None = None

    @model_validator(mode="after")
    def _verbs_match_purpose_and_credentials(self) -> Self:
        grants = self.manifest.verb_grants if self.manifest else ()
        if not grants:
            return self
        if self.verb_credentials is None:
            raise ValueError("verb grants require verb credentials")
        allowed = servers_allowed_for(self.spec.purpose)
        if any(grant.server not in allowed for grant in grants):
            raise ValueError("verb server is not allowed for this purpose")
        return self


def servers_allowed_for(purpose: str) -> frozenset[VerbServer]:
    return _SERVERS_ALLOWED_FOR_PURPOSE.get(purpose, frozenset())


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
