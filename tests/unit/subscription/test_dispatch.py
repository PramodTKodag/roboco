"""Delivery runs: supervised start, exit-code mapping, cancel, fail-closed config."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from roboco.agents_config import verify_run_token
from roboco.api.run_scope import ActiveRuns
from roboco.runtime.engines.subscription_dispatch import (
    ProjectBinding,
    SubscriptionNotConfigured,
    WorkspaceNotLinked,
    build_run_launch,
    build_run_supervisor,
    read_token_file,
)
from roboco.runtime.engines.subscription_runs import (
    FAILED_EXIT_CODE,
    LOGIN_REQUIRED_EXIT_CODE,
    RATE_LIMIT_EXIT_CODE,
    RunLaunch,
    RunSupervisor,
    UnroutableModel,
    WorkerToolsUnavailable,
    allowed_model_id,
    tool_manifest_for_role,
)
from roboco.runtime.subscription.contracts import (
    RunResult,
    RunSpec,
    RunState,
    ToolManifest,
    VerbCredentials,
    WorkerOperation,
)
from roboco.services.gateway.role_config import get_role_config

PROJECT_ID = uuid4()
WORKSPACE = uuid4()


class FakeClient:
    def __init__(self, state: RunState = RunState.COMPLETED) -> None:
        self.state = state
        self.specs: list[RunSpec] = []
        self.credentials: list[VerbCredentials | None] = []
        self.cancelled: list[UUID] = []
        self.release = asyncio.Event()
        self.release.set()

    async def run(
        self,
        spec: RunSpec,
        manifest: ToolManifest | None = None,
        verb_credentials: VerbCredentials | None = None,
    ) -> RunResult:
        self.specs.append(spec)
        self.credentials.append(verb_credentials)
        await self.release.wait()
        return RunResult(
            run_id=spec.run_id, state=self.state, text="", reason_code=None, usage={}
        )

    async def cancel(self, run_id: UUID) -> None:
        self.cancelled.append(run_id)


class FakeRegistrar:
    def __init__(self) -> None:
        self.registered: list[tuple[UUID, UUID, UUID]] = []
        self.released: list[UUID] = []

    async def register_run(
        self, run_id: UUID, project_id: UUID, workspace_handle: UUID
    ) -> None:
        self.registered.append((run_id, project_id, workspace_handle))

    async def cancel_run(self, run_id: UUID) -> None:
        self.released.append(run_id)


def _launch(*, workspace: bool = True, model: str = "sonnet") -> RunLaunch:
    role = get_role_config("developer")
    return RunLaunch(
        agent_slug="backend-dev-1",
        agent_id=uuid4(),
        role="developer",
        team="backend",
        model=model,
        prompt="synthetic prompt",
        manifest=tool_manifest_for_role(role, has_workspace=workspace),
        project_id=PROJECT_ID if workspace else None,
        workspace_handle=WORKSPACE if workspace else None,
    )


async def _settle(supervisor: RunSupervisor, slug: str) -> tuple[bool, int | None]:
    for _ in range(50):
        running, code = supervisor.state(slug)
        if not running:
            return running, code
        await asyncio.sleep(0.01)
    return supervisor.state(slug)


def _supervisor(
    client: FakeClient, registrar: FakeRegistrar | None
) -> tuple[RunSupervisor, ActiveRuns]:
    active = ActiveRuns()
    return RunSupervisor(client, registrar, active), active


async def test_run_registers_worker_activates_scope_and_releases_both() -> None:
    client, registrar = FakeClient(), FakeRegistrar()
    supervisor, active = _supervisor(client, registrar)
    handle = await supervisor.start(_launch())
    assert await _settle(supervisor, "backend-dev-1") == (False, 0)
    assert registrar.registered == [(UUID(handle), PROJECT_ID, WORKSPACE)]
    assert registrar.released == [UUID(handle)]
    assert not active.is_active(handle)
    assert client.specs[0].model == "claude-sonnet-5-5"
    assert client.specs[0].purpose == "delivery"


async def test_run_token_is_bound_to_this_run_and_agent() -> None:
    client = FakeClient()
    supervisor, _ = _supervisor(client, FakeRegistrar())
    launch = _launch()
    handle = await supervisor.start(launch)
    await _settle(supervisor, "backend-dev-1")
    credentials = client.credentials[0]
    assert credentials is not None
    claims = verify_run_token(credentials.token)
    assert claims.run == handle
    assert claims.id == str(launch.agent_id)
    assert credentials.token not in client.specs[0].prompt


async def test_scope_is_active_while_the_run_is_in_flight() -> None:
    client = FakeClient()
    client.release.clear()
    supervisor, active = _supervisor(client, FakeRegistrar())
    handle = await supervisor.start(_launch())
    assert active.is_active(handle)
    assert supervisor.state("backend-dev-1") == (True, None)
    client.release.set()
    await _settle(supervisor, "backend-dev-1")
    assert not active.is_active(handle)


@pytest.mark.parametrize(
    ("state", "code"),
    [
        (RunState.QUOTA_EXHAUSTED, RATE_LIMIT_EXIT_CODE),
        (RunState.LOGIN_REQUIRED, LOGIN_REQUIRED_EXIT_CODE),
        (RunState.BLOCKED, FAILED_EXIT_CODE),
        (RunState.FAILED, FAILED_EXIT_CODE),
    ],
)
async def test_terminal_states_map_to_the_parking_exit_codes(
    state: RunState, code: int
) -> None:
    supervisor, _ = _supervisor(FakeClient(state), FakeRegistrar())
    await supervisor.start(_launch())
    assert await _settle(supervisor, "backend-dev-1") == (False, code)


async def test_an_unroutable_model_never_starts_a_run() -> None:
    client, registrar = FakeClient(), FakeRegistrar()
    supervisor, _ = _supervisor(client, registrar)
    with pytest.raises(UnroutableModel):
        await supervisor.start(_launch(model="some-other-model"))
    assert client.specs == []
    assert registrar.registered == []
    assert not supervisor.owns("backend-dev-1")


async def test_worker_tools_without_a_launcher_refuse_before_any_scope() -> None:
    client = FakeClient()
    supervisor, active = _supervisor(client, None)
    with pytest.raises(WorkerToolsUnavailable):
        await supervisor.start(_launch())
    assert client.specs == []
    assert not active.is_active("anything")


async def test_a_run_without_a_workspace_never_touches_the_launcher() -> None:
    registrar = FakeRegistrar()
    supervisor, _ = _supervisor(FakeClient(), registrar)
    await supervisor.start(_launch(workspace=False))
    await _settle(supervisor, "backend-dev-1")
    assert registrar.registered == []
    assert registrar.released == []


async def test_a_crashing_client_is_a_failed_run_not_a_fallback() -> None:
    class Crashing(FakeClient):
        async def run(self, *args: object, **kwargs: object) -> RunResult:
            raise RuntimeError("synthetic transport crash")

    registrar = FakeRegistrar()
    supervisor, _ = _supervisor(Crashing(), registrar)
    await supervisor.start(_launch())
    assert await _settle(supervisor, "backend-dev-1") == (False, FAILED_EXIT_CODE)
    assert len(registrar.released) == 1


async def test_cancel_revokes_scope_and_stops_the_run() -> None:
    client = FakeClient()
    client.release.clear()
    supervisor, active = _supervisor(client, FakeRegistrar())
    handle = await supervisor.start(_launch())
    await supervisor.cancel("backend-dev-1")
    assert not active.is_active(handle)
    assert client.cancelled == [UUID(handle)]
    supervisor.forget("backend-dev-1")
    assert not supervisor.owns("backend-dev-1")
    await supervisor.cancel("backend-dev-1")


def test_model_tiers_resolve_to_exactly_one_allowed_id() -> None:
    assert allowed_model_id("opus") == "claude-opus-5-5"
    assert allowed_model_id("claude-haiku-5-5") == "claude-haiku-5-5"
    with pytest.raises(UnroutableModel):
        allowed_model_id("gpt-4")


def test_worker_tools_follow_role_write_access_and_workspace() -> None:
    developer = get_role_config("developer")
    qa = get_role_config("qa")
    full = tool_manifest_for_role(developer, has_workspace=True)
    assert WorkerOperation.APPLY_PATCH in full.operations
    read_only = tool_manifest_for_role(qa, has_workspace=True)
    assert WorkerOperation.APPLY_PATCH not in read_only.operations
    assert WorkerOperation.READ_FILE in read_only.operations
    assert tool_manifest_for_role(developer, has_workspace=False).operations == ()
    assert {grant.server.value for grant in full.verb_grants} == {"flow", "do"}


def test_token_files_fail_closed(tmp_path) -> None:
    with pytest.raises(SubscriptionNotConfigured):
        read_token_file("", what="x")
    with pytest.raises(SubscriptionNotConfigured):
        read_token_file(str(tmp_path / "missing"), what="x")
    empty = tmp_path / "empty"
    empty.write_text("  \n")
    with pytest.raises(SubscriptionNotConfigured):
        read_token_file(str(empty), what="x")
    big = tmp_path / "big"
    big.write_text("a" * 5000)
    with pytest.raises(SubscriptionNotConfigured):
        read_token_file(str(big), what="x")
    good = tmp_path / "good"
    good.write_text("synthetic-token\n")
    assert read_token_file(str(good), what="x") == "synthetic-token"


def test_supervisor_is_not_built_without_a_service(tmp_path) -> None:
    unset = SimpleNamespace(claude_service_host="", launcher_host="")
    with pytest.raises(SubscriptionNotConfigured):
        build_run_supervisor(unset)  # type: ignore[arg-type]
    no_token = SimpleNamespace(
        claude_service_host="claude-service",
        claude_service_port=8766,
        claude_service_token_file="",
        launcher_host="",
    )
    with pytest.raises(SubscriptionNotConfigured):
        build_run_supervisor(no_token)  # type: ignore[arg-type]


def _agent_config(tmp_path, agent_id: str = "backend-dev-1") -> SimpleNamespace:
    blueprint = tmp_path / "blueprint.md"
    blueprint.write_text("blueprint text")
    return SimpleNamespace(
        agent_id=agent_id, model="sonnet", blueprint_path=blueprint, briefing_path=None
    )


async def test_a_project_without_a_linked_workspace_is_refused(tmp_path) -> None:
    config = _agent_config(tmp_path)
    with pytest.raises(WorkspaceNotLinked):
        await build_run_launch(
            config,  # type: ignore[arg-type]
            "go",
            ProjectBinding(project_id=PROJECT_ID, workspace_handle=None),
        )


async def test_a_linked_project_gets_worker_tools_and_a_composed_prompt(
    tmp_path,
) -> None:
    launch = await build_run_launch(
        _agent_config(tmp_path),  # type: ignore[arg-type]
        "go",
        ProjectBinding(project_id=PROJECT_ID, workspace_handle=WORKSPACE),
    )
    assert launch.prompt == "blueprint text\n\ngo"
    assert launch.workspace_handle == WORKSPACE
    assert WorkerOperation.READ_FILE in launch.manifest.operations


async def test_an_agent_without_identity_is_refused(tmp_path) -> None:
    with pytest.raises(SubscriptionNotConfigured):
        await build_run_launch(
            _agent_config(tmp_path, "nobody-1"),  # type: ignore[arg-type]
            "go",
            None,
        )
