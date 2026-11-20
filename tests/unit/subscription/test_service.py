"""The trusted service driven against a scripted CLI executable."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
from roboco.runtime.subscription.cli import (
    GATEWAY_SOCKET_ENV,
    RUN_CAPABILITY_ENV,
    WORKER_MCP_SERVER_NAME,
)
from roboco.runtime.subscription.contracts import (
    RunRequest,
    RunSpec,
    RunState,
    SubscriptionState,
    SubscriptionStatus,
    ToolManifest,
    VerbCredentials,
    VerbGrant,
    VerbServer,
    WorkerJob,
    WorkerOperation,
    WorkerResult,
)
from roboco.runtime.subscription.service import ClaudeService
from roboco.runtime.subscription.tool_gateway import ToolGateway
from tests.unit.subscription.fake_cli import install_fake_cli, records, set_scenario

if TYPE_CHECKING:
    from pathlib import Path

PROMPT = "synthetic-prompt-sentinel-77c2"
ALLOWLIST = {
    "PATH",
    "HOME",
    "CLAUDE_CONFIG_DIR",
    "LANG",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "DISABLE_TELEMETRY",
    "DISABLE_ERROR_REPORTING",
    "DISABLE_AUTOUPDATER",
    "CLAUDE_CODE_MAX_RETRIES",
}


class NullExecutor:
    async def execute(self, job: WorkerJob) -> WorkerResult:
        return WorkerResult(
            job_id=job.job_id,
            exit_code=0,
            stdout="",
            stderr="",
            truncated=False,
            reason_code=None,
        )


def _status(state: SubscriptionState, reason: str | None = None) -> SubscriptionStatus:
    return SubscriptionStatus(
        state=state,
        authenticated=state is SubscriptionState.READY,
        auth_method="claude.ai" if state is SubscriptionState.READY else None,
        checked_at=datetime.now(UTC),
        reason_code=reason,
    )


class Inspector:
    def __init__(self, status: SubscriptionStatus | None = None) -> None:
        self.status = status or _status(SubscriptionState.READY)
        self.calls = 0

    async def __call__(self, **_: Any) -> SubscriptionStatus:
        self.calls += 1
        return self.status


_READ_ONLY_MANIFEST = ToolManifest(operations=(WorkerOperation.READ_FILE,))
_EXPECTED_INSPECTIONS = 2


_VERB_TOKEN = "synthetic-run-token-0123456789"
_VERB_MANIFEST = ToolManifest(
    verb_grants=(
        VerbGrant(server=VerbServer.FLOW, verbs=("i_am_done", "pass_review")),
        VerbGrant(server=VerbServer.DO, verbs=("read_task",)),
    )
)


def _verb_credentials() -> VerbCredentials:
    return VerbCredentials(agent_id=uuid4(), role="backend_dev", token=_VERB_TOKEN)


def _request(
    *,
    purpose: str = "delivery",
    manifest: ToolManifest | None = _READ_ONLY_MANIFEST,
    run_id: UUID | None = None,
    verb_credentials: VerbCredentials | None = None,
) -> RunRequest:
    delivery = purpose == "delivery"
    spec = RunSpec(
        run_id=run_id or uuid4(),
        project_id=uuid4() if delivery else None,
        purpose=purpose,  # type: ignore[arg-type]
        model="claude-sonnet-5-5",
        prompt=PROMPT,
        workspace_handle=uuid4() if delivery else None,
    )
    return RunRequest(
        spec=spec,
        manifest=manifest if delivery else None,
        verb_credentials=verb_credentials,
    )


class Harness:
    def __init__(self, root: Path, **service_options: Any) -> None:
        self.root = root
        self.cli_dir = root / "cli"
        self.cli = install_fake_cli(self.cli_dir)
        self.inspector = Inspector()
        self.gateway = ToolGateway(NullExecutor())
        (root / "empty").mkdir()
        self.runs_dir = root / "runs"
        self.service = ClaudeService(
            gateway=self.gateway,
            runtime_dir=self.runs_dir,
            socket_path=root / "gateway.sock",
            cli_path=self.cli,
            cwd=root / "empty",
            inspect=self.inspector,
            **service_options,
        )

    def scenario(self, **scenario: Any) -> None:
        set_scenario(self.cli_dir, scenario)

    def started(self) -> list[dict[str, Any]]:
        return records(self.cli_dir)

    async def wait_for_start(self, count: int = 1) -> None:
        for _ in range(200):
            if len(self.started()) >= count:
                return
            await asyncio.sleep(0.025)
        raise AssertionError("scripted CLI did not start")


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


# -- a normal run ---------------------------------------------------------


async def test_run_completes_and_the_prompt_arrives_over_stdin(
    harness: Harness,
) -> None:
    result = await harness.service.run(_request(purpose="background"))
    assert result.state is RunState.COMPLETED
    assert result.text == f"done:{len(PROMPT)}"
    assert result.usage == {"input_tokens": 3, "output_tokens": 4}
    (record,) = harness.started()
    assert record["prompt_len"] == len(PROMPT)
    assert PROMPT not in " ".join(record["argv"])


async def test_cli_starts_in_the_fixed_empty_directory(harness: Harness) -> None:
    await harness.service.run(_request(purpose="background"))
    (record,) = harness.started()
    assert os.path.realpath(record["cwd"]) == os.path.realpath(harness.root / "empty")


async def test_cli_environment_is_the_allowlist_and_never_inherited(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "HTTPS_PROXY",
        "AWS_BEARER_TOKEN_BEDROCK",
        "ROBOCO_HOST_CLAUDE_DIR",
        "GH_TOKEN",
    ):
        monkeypatch.setenv(name, "synthetic-secret-value")
    await harness.service.run(_request(purpose="background"))
    (record,) = harness.started()
    names = set(record["env_names"])
    assert names >= ALLOWLIST
    for leaked in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "HTTPS_PROXY",
        "AWS_BEARER_TOKEN_BEDROCK",
        "ROBOCO_HOST_CLAUDE_DIR",
        "GH_TOKEN",
    ):
        assert leaked not in names


async def test_tool_free_run_gets_no_mcp_config_and_no_allowed_tools(
    harness: Harness,
) -> None:
    await harness.service.run(_request(purpose="secretary"))
    (record,) = harness.started()
    assert record["mcp_config"] is None
    assert record["settings"] == {"permissions": {"allow": []}}


async def test_delivery_run_gets_only_the_bridge_and_the_granted_tool(
    harness: Harness,
) -> None:
    result = await harness.service.run(_request())
    assert result.state is RunState.COMPLETED
    (record,) = harness.started()
    assert record["settings"]["permissions"]["allow"] == [
        f"mcp__{WORKER_MCP_SERVER_NAME}__read_file"
    ]
    servers = record["mcp_config"]["mcpServers"]
    assert list(servers) == [WORKER_MCP_SERVER_NAME]
    assert set(servers[WORKER_MCP_SERVER_NAME]["env"]) == {
        "PYTHONPATH",
        GATEWAY_SOCKET_ENV,
        RUN_CAPABILITY_ENV,
    }


async def test_verb_only_run_gets_verb_servers_and_no_worker_bridge(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, orchestrator_url="http://orchestrator:8000")
    request = _request(manifest=_VERB_MANIFEST, verb_credentials=_verb_credentials())
    result = await harness.service.run(request)
    assert result.state is RunState.COMPLETED
    (record,) = harness.started()
    assert record["settings"]["permissions"]["allow"] == [
        "mcp__roboco_do__read_task",
        "mcp__roboco_flow__i_am_done",
        "mcp__roboco_flow__pass",
    ]
    servers = record["mcp_config"]["mcpServers"]
    assert set(servers) == {"roboco_flow", "roboco_do"}
    assert WORKER_MCP_SERVER_NAME not in servers


async def test_verb_credentials_stay_out_of_argv_and_the_manifest_file(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, orchestrator_url="http://orchestrator:8000")
    request = _request(manifest=_VERB_MANIFEST, verb_credentials=_verb_credentials())
    await harness.service.run(request)
    (record,) = harness.started()
    assert _VERB_TOKEN not in " ".join(record["argv"])
    assert _VERB_TOKEN not in json.dumps(record["settings"])


async def test_verb_run_without_an_orchestrator_is_refused_before_the_cli(
    harness: Harness,
) -> None:
    request = _request(manifest=_VERB_MANIFEST, verb_credentials=_verb_credentials())
    result = await harness.service.run(request)
    assert result.state is RunState.BLOCKED
    assert result.reason_code == "orchestrator_not_configured"
    assert harness.started() == []


async def test_run_files_are_removed_and_the_capability_revoked(
    harness: Harness,
) -> None:
    request = _request()
    await harness.service.run(request)
    assert list(harness.runs_dir.iterdir()) == []
    assert harness.gateway.bind(request) is not None  # the run is no longer bound


# -- refusals before the CLI starts ------------------------------------------


async def test_delivery_without_a_manifest_never_starts_the_cli(
    harness: Harness,
) -> None:
    result = await harness.service.run(_request(manifest=None))
    assert result.state is RunState.BLOCKED
    assert result.reason_code == "manifest_missing"
    assert harness.started() == []
    assert harness.inspector.calls == 0


@pytest.mark.parametrize(
    "state,reason,expected",
    [
        (SubscriptionState.LOGIN_REQUIRED, "login_required", RunState.LOGIN_REQUIRED),
        (SubscriptionState.POLICY_BLOCKED, "rejected_auth_method", RunState.BLOCKED),
        (SubscriptionState.CONFIGURATION_ERROR, "cli_unavailable", RunState.FAILED),
        (SubscriptionState.QUOTA_EXHAUSTED, "quota", RunState.QUOTA_EXHAUSTED),
    ],
)
async def test_an_unready_subscription_never_starts_the_cli(
    harness: Harness, state: SubscriptionState, reason: str, expected: RunState
) -> None:
    harness.inspector.status = _status(state, reason)
    result = await harness.service.run(_request(purpose="background"))
    assert result.state is expected
    assert result.reason_code == reason
    assert harness.started() == []


async def test_every_run_re_checks_the_subscription(harness: Harness) -> None:
    await harness.service.run(_request(purpose="background"))
    await harness.service.run(_request(purpose="background"))
    assert harness.inspector.calls == _EXPECTED_INSPECTIONS


# -- stream outcomes ----------------------------------------------------------


async def test_an_unexpected_tool_surface_blocks_the_run(harness: Harness) -> None:
    harness.scenario(mode="success", tools=["Bash", "Read"])
    result = await harness.service.run(_request())
    assert result.state is RunState.BLOCKED
    assert result.reason_code == "tool_surface_mismatch"


async def test_auth_failure_in_the_stream_requires_login(harness: Harness) -> None:
    harness.scenario(mode="auth_error")
    result = await harness.service.run(_request(purpose="background"))
    assert result.state is RunState.LOGIN_REQUIRED
    assert result.text == ""


async def test_rate_limit_in_the_stream_is_quota_exhausted(harness: Harness) -> None:
    harness.scenario(mode="rate_limit")
    result = await harness.service.run(_request(purpose="background"))
    assert result.state is RunState.QUOTA_EXHAUSTED


async def test_exceeding_the_turn_limit_kills_the_process(harness: Harness) -> None:
    harness.scenario(mode="many_turns")
    result = await harness.service.run(_request(purpose="background"))
    assert result.state is RunState.BLOCKED
    assert result.reason_code == "turn_limit"
    (record,) = harness.started()
    await _assert_process_gone(record["pid"])


async def test_missing_terminal_event_fails_with_partial_text(harness: Harness) -> None:
    harness.scenario(mode="no_result")
    result = await harness.service.run(_request(purpose="background"))
    assert result.state is RunState.FAILED
    assert result.reason_code == "no_terminal_event"
    assert result.text == "half"


async def test_cli_crash_is_a_failure_not_a_pass(harness: Harness) -> None:
    harness.scenario(mode="crash")
    result = await harness.service.run(_request(purpose="background"))
    assert result.state is RunState.FAILED


async def test_cli_stderr_never_reaches_results_or_logs(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    harness.scenario(mode="stderr_secret")
    with caplog.at_level(logging.DEBUG):
        result = await harness.service.run(_request(purpose="background"))
    assert "SYNTHETIC-STDERR-SECRET-5521" not in result.model_dump_json()
    assert "SYNTHETIC-STDERR-SECRET-5521" not in caplog.text
    assert PROMPT not in caplog.text


# -- limits, cancellation, serialization ----------------------------------------


async def test_run_time_limit_kills_the_process_group(tmp_path: Path) -> None:
    harness = Harness(tmp_path, run_seconds=0.6)
    harness.scenario(mode="sleep")
    result = await harness.service.run(_request(purpose="background"))
    assert result.state is RunState.BLOCKED
    assert result.reason_code == "run_time_limit"
    (record,) = harness.started()
    await _assert_process_gone(record["pid"])


async def test_cancelling_a_running_run_stops_it_and_revokes_its_tools(
    harness: Harness,
) -> None:
    harness.scenario(mode="sleep")
    request = _request()
    task = asyncio.create_task(harness.service.run(request))
    await harness.wait_for_start()
    assert await harness.service.cancel(request.spec.run_id) is True
    result = await asyncio.wait_for(task, 10)
    assert result.state is RunState.CANCELLED
    (record,) = harness.started()
    await _assert_process_gone(record["pid"])
    assert harness.gateway.bind(request) is not None  # capability was revoked


async def test_cancelling_an_unknown_run_reports_false(harness: Harness) -> None:
    assert await harness.service.cancel(uuid4()) is False


async def test_a_queued_run_cancelled_before_it_starts_never_starts(
    harness: Harness,
) -> None:
    harness.scenario(mode="sleep")
    first, second = _request(purpose="background"), _request(purpose="background")
    first_task = asyncio.create_task(harness.service.run(first))
    await harness.wait_for_start()
    second_task = asyncio.create_task(harness.service.run(second))
    await asyncio.sleep(0.1)
    await harness.service.cancel(second.spec.run_id)
    await harness.service.cancel(first.spec.run_id)
    assert (await asyncio.wait_for(second_task, 10)).state is RunState.CANCELLED
    await asyncio.wait_for(first_task, 10)
    assert len(harness.started()) == 1


async def test_runs_never_overlap_for_one_credential_profile(
    harness: Harness,
) -> None:
    harness.scenario(mode="slow", seconds=0.3)
    await asyncio.gather(
        *(harness.service.run(_request(purpose="background")) for _ in range(3))
    )
    spans = (harness.cli_dir / "spans.log").read_text().split("\n")
    kinds = [line.split()[0] for line in spans if line]
    assert kinds == ["start", "end"] * 3


async def test_auth_inspection_waits_for_the_credential_lock(harness: Harness) -> None:
    harness.scenario(mode="sleep")
    request = _request(purpose="background")
    run_task = asyncio.create_task(harness.service.run(request))
    await harness.wait_for_start()
    status_task = asyncio.create_task(harness.service.status())
    await asyncio.sleep(0.2)
    assert not status_task.done()
    await harness.service.cancel(request.spec.run_id)
    await asyncio.wait_for(run_task, 10)
    assert (await asyncio.wait_for(status_task, 10)).state is SubscriptionState.READY


async def test_a_full_queue_refuses_new_runs(tmp_path: Path) -> None:
    harness = Harness(tmp_path, max_queued=1)
    harness.scenario(mode="sleep")
    running = _request(purpose="background")
    queued = _request(purpose="background")
    tasks = [asyncio.create_task(harness.service.run(running))]
    await harness.wait_for_start()
    tasks.append(asyncio.create_task(harness.service.run(queued)))
    await asyncio.sleep(0.1)
    overflow = await harness.service.run(_request(purpose="background"))
    assert overflow.state is RunState.BLOCKED
    assert overflow.reason_code == "queue_full"
    for request in (queued, running):
        await harness.service.cancel(request.spec.run_id)
    await asyncio.wait_for(asyncio.gather(*tasks), 10)


async def test_the_same_run_cannot_be_active_twice(harness: Harness) -> None:
    harness.scenario(mode="sleep")
    request = _request(purpose="background")
    task = asyncio.create_task(harness.service.run(request))
    await harness.wait_for_start()
    duplicate = await harness.service.run(request)
    assert duplicate.reason_code == "run_already_active"
    await harness.service.cancel(request.spec.run_id)
    await asyncio.wait_for(task, 10)


async def _assert_process_gone(pid: int) -> None:
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"process {pid} is still alive")
