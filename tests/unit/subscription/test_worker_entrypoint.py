"""Worker entrypoint: runs project commands with a clean env and bounded output."""

from __future__ import annotations

import asyncio
import io
import json
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from roboco.runtime.subscription.contracts import (
    MAX_STREAM_BYTES,
    WorkerJob,
    WorkerOperation,
    WorkerResult,
)
from roboco.runtime.subscription.worker_entrypoint import execute_job, run_stdio

if TYPE_CHECKING:
    from pathlib import Path


def _command_job(command: str) -> WorkerJob:
    return WorkerJob(
        job_id=uuid4(),
        run_id=uuid4(),
        project_id=uuid4(),
        workspace_handle=uuid4(),
        operation=WorkerOperation.RUN_COMMAND,
        payload={"operation": "run_command", "command": command},  # type: ignore[arg-type]
    )


async def test_command_runs_in_the_workspace_and_reports_exit_code(
    tmp_path: Path,
) -> None:
    (tmp_path / "marker.txt").write_text("here")
    result = await execute_job(_command_job("cat marker.txt; exit 3"), tmp_path, 10)
    assert result.stdout == "here"
    assert result.exit_code == 3  # noqa: PLR2004
    assert result.reason_code is None
    assert result.truncated is False


async def test_command_environment_is_a_clean_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SYNTHETIC-leak")
    monkeypatch.setenv("SOME_TOKEN", "leak")
    result = await execute_job(_command_job("env"), tmp_path, 10)
    names = {line.split("=", 1)[0] for line in result.stdout.splitlines()}
    assert "ANTHROPIC_API_KEY" not in names
    assert "SOME_TOKEN" not in names
    assert "leak" not in result.stdout
    assert {"PATH", "HOME"} <= names


async def test_output_is_bounded_and_marked_truncated(tmp_path: Path) -> None:
    result = await execute_job(
        _command_job("head -c 300000 /dev/zero | tr '\\0' 'a'"), tmp_path, 10
    )
    assert len(result.stdout.encode()) <= MAX_STREAM_BYTES
    assert result.truncated is True
    assert result.exit_code == 0


async def test_invalid_utf8_output_never_exceeds_the_limit(tmp_path: Path) -> None:
    result = await execute_job(
        _command_job("head -c 200000 /dev/urandom"), tmp_path, 10
    )
    assert len(result.stdout.encode()) <= MAX_STREAM_BYTES
    assert result.truncated is True


async def test_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "child-survived"
    result = await execute_job(
        _command_job(f"(sleep 2; touch {marker}) & sleep 30"), tmp_path, 0.5
    )
    assert result.reason_code == "command_timeout"
    assert result.exit_code is None
    await asyncio.sleep(2.5)
    assert not marker.exists()


async def test_operations_not_yet_supported_fail_explicitly(tmp_path: Path) -> None:
    job = WorkerJob(
        job_id=uuid4(),
        run_id=uuid4(),
        project_id=uuid4(),
        workspace_handle=uuid4(),
        operation=WorkerOperation.EXPORT_PATCH,
        payload={"operation": "export_patch"},  # type: ignore[arg-type]
    )
    result = await execute_job(job, tmp_path, 10)
    assert result.reason_code == "operation_unsupported"
    assert result.exit_code is None


async def test_stdio_round_trip_returns_one_result_line(tmp_path: Path) -> None:
    job = _command_job("echo hi")
    stdin = io.StringIO(job.model_dump_json() + "\n")
    stdout = io.StringIO()
    await run_stdio(stdin, stdout, tmp_path, 10)
    result = WorkerResult.model_validate_json(stdout.getvalue())
    assert result.job_id == job.job_id
    assert result.stdout.strip() == "hi"


@pytest.mark.parametrize(
    "line", ["", "not json", '{"image": "evil"}', "[]", "x" * (3 * 1024 * 1024)]
)
async def test_stdio_rejects_invalid_jobs_without_running_anything(
    tmp_path: Path, line: str
) -> None:
    stdout = io.StringIO()
    await run_stdio(io.StringIO(line + "\n"), stdout, tmp_path, 10)
    payload = json.loads(stdout.getvalue())
    assert payload["reason_code"] == "job_invalid"
    assert payload["exit_code"] is None
