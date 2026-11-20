"""Controller-side project executor: every outcome is a real result or an error.

An infrastructure failure must never look like a passing or skipped command.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from roboco.runtime.subscription.contracts import (
    MAX_COMMAND_BYTES,
    MAX_CONVENTION_FILES,
    WorkerJob,
    WorkerResult,
)
from roboco.runtime.subscription.project_executor import (
    CommandOutcome,
    ProjectExecutionUnavailable,
    UnconfiguredProjectExecutorProvider,
    WorkerProjectExecutor,
)


class _FakeLauncher:
    def __init__(self, result: WorkerResult | None = None) -> None:
        self.jobs: list[WorkerJob] = []
        self._result = result

    async def execute(self, job: WorkerJob) -> WorkerResult:
        self.jobs.append(job)
        if self._result is not None:
            return self._result
        return _result(job.job_id, exit_code=0)


def _result(
    job_id: UUID,
    *,
    exit_code: int | None,
    stdout: str = "",
    stderr: str = "",
    reason_code: str | None = None,
) -> WorkerResult:
    return WorkerResult(
        job_id=job_id,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        truncated=False,
        reason_code=reason_code,
    )


def _executor(launcher: _FakeLauncher) -> WorkerProjectExecutor:
    return WorkerProjectExecutor(
        launcher,
        run_id=uuid4(),
        project_id=uuid4(),
        workspace_handle=uuid4(),
    )


@pytest.mark.asyncio
async def test_command_is_sent_as_one_run_command_job_for_the_bound_workspace() -> None:
    launcher = _FakeLauncher()
    executor = _executor(launcher)
    await executor.run_command("make gate")
    (job,) = launcher.jobs
    assert job.operation.value == "run_command"
    assert job.payload.command == "make gate"  # type: ignore[union-attr]
    assert job.run_id == executor.run_id
    assert job.project_id == executor.project_id
    assert job.workspace_handle == executor.workspace_handle


@pytest.mark.asyncio
async def test_each_command_gets_a_fresh_job_id() -> None:
    launcher = _FakeLauncher()
    executor = _executor(launcher)
    await executor.run_command("a")
    await executor.run_command("b")
    assert launcher.jobs[0].job_id != launcher.jobs[1].job_id


@pytest.mark.asyncio
async def test_exit_status_and_combined_output_are_returned() -> None:
    job_ids: list[UUID] = []

    class _Launcher(_FakeLauncher):
        async def execute(self, job: WorkerJob) -> WorkerResult:
            job_ids.append(job.job_id)
            return _result(job.job_id, exit_code=3, stdout="out\n", stderr="err\n")

    outcome = await _executor(_Launcher()).run_command("x")
    assert outcome == CommandOutcome(exit_code=3, output="out\nerr\n", truncated=False)


@pytest.mark.asyncio
async def test_command_timeout_is_a_failed_command_not_an_infrastructure_error() -> (
    None
):
    class _Launcher(_FakeLauncher):
        async def execute(self, job: WorkerJob) -> WorkerResult:
            return _result(job.job_id, exit_code=None, reason_code="command_timeout")

    outcome = await _executor(_Launcher()).run_command("sleep 9999")
    assert outcome.exit_code != 0
    assert "timed out" in outcome.output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        "launcher_unreachable",
        "run_cancelled",
        "run_expired",
        "worker_timeout",
        "worker_result_invalid",
        "queue_full",
        "unauthorized",
        "output_drain_timeout",
    ],
)
async def test_infrastructure_failures_raise_instead_of_returning_an_outcome(
    reason: str,
) -> None:
    class _Launcher(_FakeLauncher):
        async def execute(self, job: WorkerJob) -> WorkerResult:
            return _result(job.job_id, exit_code=None, reason_code=reason)

    with pytest.raises(ProjectExecutionUnavailable) as raised:
        await _executor(_Launcher()).run_command("make gate")
    assert raised.value.reason_code == reason


@pytest.mark.asyncio
async def test_result_without_exit_code_or_reason_is_never_a_pass() -> None:
    class _Launcher(_FakeLauncher):
        async def execute(self, job: WorkerJob) -> WorkerResult:
            return _result(job.job_id, exit_code=None)

    with pytest.raises(ProjectExecutionUnavailable) as raised:
        await _executor(_Launcher()).run_command("make gate")
    assert raised.value.reason_code == "worker_result_invalid"


@pytest.mark.asyncio
async def test_exit_code_with_a_reason_is_treated_as_malformed() -> None:
    class _Launcher(_FakeLauncher):
        async def execute(self, job: WorkerJob) -> WorkerResult:
            return _result(job.job_id, exit_code=0, reason_code="something")

    with pytest.raises(ProjectExecutionUnavailable):
        await _executor(_Launcher()).run_command("make gate")


@pytest.mark.asyncio
async def test_result_for_a_different_job_is_rejected() -> None:
    class _Launcher(_FakeLauncher):
        async def execute(self, _job: WorkerJob) -> WorkerResult:
            return _result(uuid4(), exit_code=0)

    with pytest.raises(ProjectExecutionUnavailable) as raised:
        await _executor(_Launcher()).run_command("make gate")
    assert raised.value.reason_code == "worker_result_mismatch"


@pytest.mark.asyncio
async def test_oversized_command_is_refused_before_reaching_the_launcher() -> None:
    launcher = _FakeLauncher()
    with pytest.raises(ProjectExecutionUnavailable) as raised:
        await _executor(launcher).run_command("x" * (MAX_COMMAND_BYTES + 1))
    assert raised.value.reason_code == "command_rejected"
    assert launcher.jobs == []


@pytest.mark.asyncio
async def test_unconfigured_provider_refuses_every_project() -> None:
    with pytest.raises(ProjectExecutionUnavailable) as raised:
        await UnconfiguredProjectExecutorProvider().executor_for(uuid4(), uuid4())
    assert raised.value.reason_code == "worker_not_configured"


@pytest.mark.asyncio
async def test_conventions_check_sends_the_files_and_returns_json_lines() -> None:
    sent: list[WorkerJob] = []

    class _Launcher(_FakeLauncher):
        async def execute(self, job: WorkerJob) -> WorkerResult:
            sent.append(job)
            return _result(
                job.job_id, exit_code=0, stdout='{"rule":"r"}\n{"rule":"s"}\n'
            )

    outcome = await _executor(_Launcher()).check_conventions(["a.py", "b.ts"])
    assert sent[0].operation.value == "check_conventions"
    assert sent[0].payload.files == ["a.py", "b.ts"]  # type: ignore[union-attr]
    assert outcome.could_not_run is False
    assert outcome.finding_lines == ('{"rule":"r"}', '{"rule":"s"}')


@pytest.mark.asyncio
async def test_conventions_check_exit_three_is_could_not_run_with_a_reason() -> None:
    class _Launcher(_FakeLauncher):
        async def execute(self, job: WorkerJob) -> WorkerResult:
            return _result(job.job_id, exit_code=3, stderr='{"error": "no grammar"}')

    outcome = await _executor(_Launcher()).check_conventions(["a.py"])
    assert outcome.could_not_run is True
    assert "no grammar" in (outcome.reason or "")


@pytest.mark.asyncio
async def test_conventions_check_infrastructure_failure_raises() -> None:
    class _Launcher(_FakeLauncher):
        async def execute(self, job: WorkerJob) -> WorkerResult:
            return _result(job.job_id, exit_code=None, reason_code="run_cancelled")

    with pytest.raises(ProjectExecutionUnavailable):
        await _executor(_Launcher()).check_conventions(["a.py"])


@pytest.mark.asyncio
async def test_conventions_check_batches_large_file_lists_and_merges_findings() -> None:
    sizes: list[int] = []

    class _Launcher(_FakeLauncher):
        async def execute(self, job: WorkerJob) -> WorkerResult:
            sizes.append(len(job.payload.files))  # type: ignore[union-attr]
            return _result(job.job_id, exit_code=0, stdout=f'{{"batch":{len(sizes)}}}')

    files = [f"f{i}.py" for i in range(MAX_CONVENTION_FILES * 2 + 1)]
    outcome = await _executor(_Launcher()).check_conventions(files)
    assert sizes == [MAX_CONVENTION_FILES, MAX_CONVENTION_FILES, 1]
    assert outcome.finding_lines == ('{"batch":1}', '{"batch":2}', '{"batch":3}')


@pytest.mark.asyncio
async def test_conventions_check_truncated_findings_are_could_not_run() -> None:
    class _Launcher(_FakeLauncher):
        async def execute(self, job: WorkerJob) -> WorkerResult:
            return WorkerResult(
                job_id=job.job_id,
                exit_code=0,
                stdout='{"rule":"r"}',
                stderr="",
                truncated=True,
                reason_code=None,
            )

    outcome = await _executor(_Launcher()).check_conventions(["a.py"])
    assert outcome.could_not_run is True
    assert outcome.finding_lines == ()


@pytest.mark.asyncio
async def test_conventions_check_stops_at_the_first_batch_that_could_not_run() -> None:
    calls = 0

    class _Launcher(_FakeLauncher):
        async def execute(self, job: WorkerJob) -> WorkerResult:
            nonlocal calls
            calls += 1
            return _result(job.job_id, exit_code=3, stderr="no grammar")

    files = [f"f{i}.py" for i in range(MAX_CONVENTION_FILES + 1)]
    outcome = await _executor(_Launcher()).check_conventions(files)
    assert outcome.could_not_run is True
    assert calls == 1
