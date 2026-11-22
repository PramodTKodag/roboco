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
from roboco.runtime.subscription.worker_patch import init_baseline

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


def _file_job(operation: str, **fields: object) -> WorkerJob:
    return WorkerJob(
        job_id=uuid4(),
        run_id=uuid4(),
        project_id=uuid4(),
        workspace_handle=uuid4(),
        operation=WorkerOperation(operation),
        payload={"operation": operation, **fields},  # type: ignore[arg-type]
    )


async def test_file_operations_dispatch_to_the_safe_file_layer(
    tmp_path: Path,
) -> None:
    written = await execute_job(
        _file_job("write_file", path="src/a.txt", content="hello needle\n"),
        tmp_path,
        10,
    )
    assert written.reason_code is None
    read = await execute_job(_file_job("read_file", path="src/a.txt"), tmp_path, 10)
    assert read.stdout == "hello needle\n"
    listed = await execute_job(_file_job("list_files", path="."), tmp_path, 10)
    assert listed.stdout == "src/"
    found = await execute_job(
        _file_job("search_text", pattern="needle", path="."), tmp_path, 10
    )
    assert found.stdout == "src/a.txt:1:hello needle"


async def test_refused_file_operations_return_reason_codes_not_exceptions(
    tmp_path: Path,
) -> None:
    denied = await execute_job(_file_job("read_file", path=".env"), tmp_path, 10)
    assert denied.reason_code == "path_denied"
    assert denied.exit_code is None
    missing = await execute_job(_file_job("read_file", path="nope"), tmp_path, 10)
    assert missing.reason_code == "not_found"


async def test_export_patch_runs_against_the_local_baseline(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("one\n")
    init_baseline(tmp_path)
    (tmp_path / "a.txt").write_text("two\n")
    result = await execute_job(_file_job("export_patch"), tmp_path, 10)
    assert "+two" in result.stdout
    assert result.reason_code is None


async def test_accept_changes_advances_the_baseline_for_the_next_export(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.txt").write_text("one\n")
    init_baseline(tmp_path)
    (tmp_path / "a.txt").write_text("two\n")
    accepted = await execute_job(_file_job("accept_changes"), tmp_path, 10)
    assert accepted.exit_code == 0
    assert accepted.reason_code is None
    exported = await execute_job(_file_job("export_patch"), tmp_path, 10)
    assert exported.stdout == ""


async def test_accept_changes_without_a_repository_reports_a_reason(
    tmp_path: Path,
) -> None:
    result = await execute_job(_file_job("accept_changes"), tmp_path, 10)
    assert result.reason_code == "accept_failed"
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


def _conventions_job(files: list[str]) -> WorkerJob:
    return WorkerJob(
        job_id=uuid4(),
        run_id=uuid4(),
        project_id=uuid4(),
        workspace_handle=uuid4(),
        operation=WorkerOperation.CHECK_CONVENTIONS,
        payload={"operation": "check_conventions", "files": files},  # type: ignore[arg-type]
    )


def _seed_router_model(root: Path) -> None:
    routers = root / "app" / "routers"
    routers.mkdir(parents=True)
    (routers / "u.py").write_text(
        "from pydantic import BaseModel\nclass M(BaseModel):\n    x: int\n"
    )
    (root / ".roboco").mkdir()
    (root / ".roboco" / "conventions.yml").write_text(
        "modules:\n  - path: app/routers\n    purpose: r\n    forbidden: [model]\n"
    )


async def test_check_conventions_returns_findings_as_json_lines(
    tmp_path: Path,
) -> None:
    _seed_router_model(tmp_path)
    result = await execute_job(_conventions_job(["app/routers/u.py"]), tmp_path, 30)
    assert result.exit_code == 0
    assert result.reason_code is None
    assert json.loads(result.stdout.splitlines()[0])["rule"] == "no_models_in_routers"


async def test_check_conventions_reports_could_not_run_as_exit_three(
    tmp_path: Path,
) -> None:
    (tmp_path / ".roboco").mkdir()
    (tmp_path / ".roboco" / "conventions.yml").write_text("modules: [oops\n")
    result = await execute_job(_conventions_job(["a.py"]), tmp_path, 30)
    assert result.exit_code == 3  # noqa: PLR2004
    assert "unparseable" in result.stderr
    assert result.reason_code is None


async def test_check_conventions_never_runs_project_code(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    (tmp_path / "evil.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
    (tmp_path / "conftest.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
    await execute_job(_conventions_job(["evil.py"]), tmp_path, 30)
    assert not marker.exists()


async def test_check_conventions_refuses_a_denied_path(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TOKEN=synthetic-value\n")
    result = await execute_job(_conventions_job([".env"]), tmp_path, 30)
    assert result.reason_code == "path_denied"
    assert "synthetic-value" not in result.stdout + result.stderr
