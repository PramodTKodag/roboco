"""Run-scoped tool authority: cross-run, stale, escalation and model-supplied IDs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from roboco.runtime.subscription.contracts import (
    CONTROLLER_ONLY_OPERATIONS,
    MAX_RUN_SECONDS,
    RunRequest,
    RunSpec,
    ToolManifest,
    VerbCredentials,
    VerbGrant,
    VerbServer,
    WorkerJob,
    WorkerOperation,
    WorkerResult,
)
from roboco.runtime.subscription.tool_gateway import ToolDenied, ToolGateway

READ_ONLY = (WorkerOperation.READ_FILE, WorkerOperation.LIST_FILES)
ALL_OPERATIONS = tuple(
    op for op in WorkerOperation if op not in CONTROLLER_ONLY_OPERATIONS
)


class RecordingExecutor:
    def __init__(self) -> None:
        self.jobs: list[WorkerJob] = []

    async def execute(self, job: WorkerJob) -> WorkerResult:
        self.jobs.append(job)
        return WorkerResult(
            job_id=job.job_id,
            exit_code=0,
            stdout="ok",
            stderr="",
            truncated=False,
            reason_code=None,
        )


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 20, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _request(
    *,
    purpose: str = "delivery",
    operations: tuple[WorkerOperation, ...] | None = READ_ONLY,
    run_id: UUID | None = None,
    with_workspace: bool = True,
) -> RunRequest:
    spec = RunSpec(
        run_id=run_id or uuid4(),
        project_id=uuid4() if with_workspace else None,
        purpose=purpose,  # type: ignore[arg-type]
        model="claude-sonnet-5-5",
        prompt="do the work",
        workspace_handle=uuid4() if with_workspace else None,
    )
    manifest = None if operations is None else ToolManifest(operations=operations)
    return RunRequest(spec=spec, manifest=manifest)


@pytest.fixture
def executor() -> RecordingExecutor:
    return RecordingExecutor()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def gateway(executor: RecordingExecutor, clock: Clock) -> ToolGateway:
    return ToolGateway(executor, clock=clock)


def _bind(gateway: ToolGateway, request: RunRequest) -> str:
    capability = gateway.bind(request)
    assert capability is not None
    return capability


# -- missing / invalid manifests ------------------------------------------


@pytest.mark.parametrize("operations", [None, ()])
def test_delivery_run_without_a_manifest_is_refused(
    gateway: ToolGateway, operations: tuple[WorkerOperation, ...] | None
) -> None:
    with pytest.raises(ToolDenied) as denied:
        gateway.bind(_request(operations=operations))
    assert denied.value.reason_code == "manifest_missing"


def test_delivery_run_without_a_workspace_is_refused(gateway: ToolGateway) -> None:
    with pytest.raises(ToolDenied) as denied:
        gateway.bind(_request(with_workspace=False))
    assert denied.value.reason_code == "workspace_required"


def test_manifest_with_duplicate_operations_is_invalid() -> None:
    with pytest.raises(ValueError, match="unique"):
        ToolManifest(operations=(WorkerOperation.READ_FILE, WorkerOperation.READ_FILE))


def test_manifest_with_an_unknown_operation_is_invalid() -> None:
    with pytest.raises(ValueError, match="operations"):
        ToolManifest.model_validate({"operations": ["delete_everything"]})


@pytest.mark.parametrize("purpose", ["intake", "secretary", "background"])
def test_non_delivery_purposes_never_inherit_delivery_tools(
    gateway: ToolGateway, purpose: str
) -> None:
    request = _request(purpose=purpose, operations=None, with_workspace=False)
    assert gateway.bind(request) is None
    with pytest.raises(ToolDenied) as denied:
        gateway.bind(_request(purpose=purpose, operations=ALL_OPERATIONS))
    assert denied.value.reason_code == "tools_not_allowed_for_purpose"


@pytest.mark.parametrize("purpose", ["secretary", "background"])
def test_purposes_without_files_get_no_worker_operations(
    gateway: ToolGateway, purpose: str
) -> None:
    with pytest.raises(ToolDenied) as denied:
        gateway.bind(_request(purpose=purpose, operations=READ_ONLY))
    assert denied.value.reason_code == "tools_not_allowed_for_purpose"


def test_intake_may_read_but_never_write(gateway: ToolGateway) -> None:
    assert gateway.bind(_request(purpose="intake", operations=READ_ONLY)) is not None
    readable = {
        WorkerOperation.READ_FILE,
        WorkerOperation.LIST_FILES,
        WorkerOperation.SEARCH_TEXT,
    }
    for operation in set(ALL_OPERATIONS) - readable:
        with pytest.raises(ToolDenied) as denied:
            gateway.bind(_request(purpose="intake", operations=(operation,)))
        assert denied.value.reason_code == "tools_not_allowed_for_purpose"


def test_a_verb_only_delivery_run_needs_no_worker_capability(
    gateway: ToolGateway,
) -> None:
    request = RunRequest(
        spec=RunSpec(
            run_id=uuid4(),
            purpose="delivery",
            model="claude-sonnet-5-5",
            prompt="p",
        ),
        manifest=ToolManifest(
            verb_grants=(VerbGrant(server=VerbServer.FLOW, verbs=("i_am_done",)),)
        ),
        verb_credentials=VerbCredentials(
            agent_id=uuid4(), role="backend_dev", token="synthetic-run-token-0123456789"
        ),
    )
    assert gateway.bind(request) is None


def test_validate_changes_nothing(gateway: ToolGateway) -> None:
    request = _request()
    gateway.validate(request)
    assert gateway.bind(request) is not None


# -- authorization from the registered run only ----------------------------


async def test_job_is_bound_to_the_registered_run_not_the_model(
    gateway: ToolGateway, executor: RecordingExecutor
) -> None:
    request = _request()
    capability = _bind(gateway, request)
    result = await gateway.invoke(capability, "read_file", {"path": "a.py"})
    assert result.stdout == "ok"
    (job,) = executor.jobs
    assert job.run_id == request.spec.run_id
    assert job.project_id == request.spec.project_id
    assert job.workspace_handle == request.spec.workspace_handle
    assert job.operation is WorkerOperation.READ_FILE


@pytest.mark.parametrize(
    "smuggled",
    [
        {"run_id": str(uuid4())},
        {"project_id": str(uuid4())},
        {"workspace_handle": str(uuid4())},
        {"job_id": str(uuid4())},
        {"operation": "write_file"},
        {"capability": "x"},
        {"image": "evil"},
        {"env": {"A": "b"}},
    ],
)
async def test_model_supplied_identity_fields_are_rejected(
    gateway: ToolGateway, executor: RecordingExecutor, smuggled: dict[str, Any]
) -> None:
    capability = _bind(gateway, _request())
    with pytest.raises(ToolDenied) as denied:
        await gateway.invoke(capability, "read_file", {"path": "a.py", **smuggled})
    assert denied.value.reason_code == "arguments_invalid"
    assert executor.jobs == []


async def test_cross_run_job_is_denied(
    gateway: ToolGateway, executor: RecordingExecutor
) -> None:
    first, second = _request(), _request()
    first_capability = _bind(gateway, first)
    _bind(gateway, second)
    await gateway.invoke(first_capability, "read_file", {"path": "a.py"})
    assert [job.run_id for job in executor.jobs] == [first.spec.run_id]
    with pytest.raises(ToolDenied) as denied:
        await gateway.invoke(
            first_capability,
            "read_file",
            {"path": "a.py", "run_id": str(second.spec.run_id)},
        )
    assert denied.value.reason_code == "arguments_invalid"


async def test_an_unknown_or_guessed_capability_is_denied(
    gateway: ToolGateway, executor: RecordingExecutor
) -> None:
    _bind(gateway, _request())
    for guess in ("", "guess", "x" * 43):
        with pytest.raises(ToolDenied) as denied:
            await gateway.invoke(guess, "read_file", {"path": "a.py"})
        assert denied.value.reason_code == "capability_unknown"
    assert executor.jobs == []


# -- stale capability replay -------------------------------------------------


async def test_revoked_capability_cannot_be_replayed(
    gateway: ToolGateway, executor: RecordingExecutor
) -> None:
    request = _request()
    capability = _bind(gateway, request)
    gateway.revoke(request.spec.run_id)
    with pytest.raises(ToolDenied) as denied:
        await gateway.invoke(capability, "read_file", {"path": "a.py"})
    assert denied.value.reason_code == "capability_unknown"
    assert executor.jobs == []


async def test_expired_capability_is_denied_and_removed(
    gateway: ToolGateway, clock: Clock
) -> None:
    capability = _bind(gateway, _request())
    clock.now += timedelta(seconds=MAX_RUN_SECONDS + 1)
    with pytest.raises(ToolDenied) as denied:
        await gateway.invoke(capability, "read_file", {"path": "a.py"})
    assert denied.value.reason_code == "capability_expired"
    with pytest.raises(ToolDenied) as again:
        await gateway.invoke(capability, "read_file", {"path": "a.py"})
    assert again.value.reason_code == "capability_unknown"


def test_a_run_cannot_be_bound_twice(gateway: ToolGateway) -> None:
    request = _request()
    _bind(gateway, request)
    with pytest.raises(ToolDenied) as denied:
        gateway.bind(request)
    assert denied.value.reason_code == "run_already_bound"


def test_a_revoked_run_can_be_bound_again_with_a_new_capability(
    gateway: ToolGateway,
) -> None:
    request = _request()
    first = _bind(gateway, request)
    gateway.revoke(request.spec.run_id)
    assert _bind(gateway, request) != first


# -- role escalation -------------------------------------------------------


@pytest.mark.parametrize(
    "operation,arguments",
    [
        ("write_file", {"path": "a.py", "content": "x"}),
        ("apply_patch", {"patch": "diff"}),
        ("run_command", {"command": "id"}),
        ("export_patch", {}),
    ],
)
async def test_read_only_grant_cannot_escalate(
    gateway: ToolGateway,
    executor: RecordingExecutor,
    operation: str,
    arguments: dict[str, Any],
) -> None:
    capability = _bind(gateway, _request(operations=READ_ONLY))
    with pytest.raises(ToolDenied) as denied:
        await gateway.invoke(capability, operation, arguments)
    assert denied.value.reason_code == "operation_not_granted"
    assert executor.jobs == []


async def test_unknown_operation_is_denied(gateway: ToolGateway) -> None:
    capability = _bind(gateway, _request())
    for name in ("shell", "READ_FILE", "read_file ", ""):
        with pytest.raises(ToolDenied) as denied:
            await gateway.invoke(capability, name, {})
        assert denied.value.reason_code == "unknown_operation"


def test_operations_for_lists_exactly_the_grant(gateway: ToolGateway) -> None:
    capability = _bind(gateway, _request(operations=READ_ONLY))
    assert set(gateway.operations_for(capability)) == set(READ_ONLY)


async def test_invalid_arguments_for_a_granted_operation_are_denied(
    gateway: ToolGateway, executor: RecordingExecutor
) -> None:
    capability = _bind(gateway, _request(operations=ALL_OPERATIONS))
    for operation, arguments in (
        ("read_file", {"path": "../escape"}),
        ("read_file", {"path": "/etc/passwd"}),
        ("write_file", {"path": "a.py"}),
        ("run_command", {"command": ""}),
    ):
        with pytest.raises(ToolDenied) as denied:
            await gateway.invoke(capability, operation, arguments)
        assert denied.value.reason_code == "arguments_invalid"
    assert executor.jobs == []
