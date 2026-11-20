"""Entrypoint of the credential-free, offline task worker.

Reads exactly one job (a JSON line on stdin), executes it with a clean
environment inside the workspace, and writes exactly one result line on stdout.
It runs only inside the worker container: no network, no credentials, no
control socket. The interpreter starts in ``/`` so project files can never
shadow imports or inject configuration into this process.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from pathlib import Path
from typing import BinaryIO, TextIO
from uuid import UUID

from pydantic import ValidationError

from roboco.runtime.subscription.contracts import (
    COMMAND_TIMEOUT_SECONDS,
    IMPORT_MODE_ARGUMENT,
    MAX_IMPORT_REPORT_ITEMS,
    MAX_STREAM_BYTES,
    ApplyPatchPayload,
    ExportPatchPayload,
    ImportFinding,
    ImportOptions,
    ImportOutcome,
    ListFilesPayload,
    ReadFilePayload,
    RunCommandPayload,
    SearchTextPayload,
    WorkerJob,
    WorkerResult,
    WriteFilePayload,
)
from roboco.runtime.subscription.launcher_protocol import MAX_FRAME_BYTES
from roboco.runtime.subscription.worker_files import (
    FileOutput,
    WorkspaceFileError,
    list_files,
    read_file,
    search_text,
    write_file,
)
from roboco.runtime.subscription.worker_patch import (
    apply_patch,
    export_patch,
    init_baseline,
)
from roboco.runtime.subscription.workspace_admission import (
    AdmissionRefused,
    admit_archive,
)

logger = logging.getLogger("roboco.worker")

WORKSPACE = Path("/workspace")
_NIL_JOB = UUID(int=0)
_READ_CHUNK = 16 * 1024
_DRAIN_GRACE_SECONDS = 5.0
_COMMAND_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/tmp",  # nosec B108 - container tmpfs, not a host path
    "LANG": "C.UTF-8",
}


def _failure(job_id: UUID, reason_code: str) -> WorkerResult:
    return WorkerResult(
        job_id=job_id,
        exit_code=None,
        stdout="",
        stderr="",
        truncated=False,
        reason_code=reason_code,
    )


def _decode_bounded(raw: bytes) -> str:
    """Decode to text that re-encodes to at most MAX_STREAM_BYTES."""
    text = raw[:MAX_STREAM_BYTES].decode("utf-8", errors="replace")
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_STREAM_BYTES:
        text = encoded[:MAX_STREAM_BYTES].decode("utf-8", errors="ignore")
    return text


async def _read_bounded(stream: asyncio.StreamReader) -> tuple[bytes, bool]:
    """Keep the first MAX_STREAM_BYTES; keep draining so the child never blocks."""
    kept = bytearray()
    truncated = False
    while chunk := await stream.read(_READ_CHUNK):
        room = MAX_STREAM_BYTES - len(kept)
        if room > 0:
            kept.extend(chunk[:room])
        if len(chunk) > room:
            truncated = True
    return bytes(kept), truncated


def _kill_group(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


async def _run_command(
    job: WorkerJob, command: str, workspace: Path, timeout_seconds: float
) -> WorkerResult:
    process = await asyncio.create_subprocess_exec(
        "/bin/sh",
        "-c",
        command,
        cwd=workspace,
        env=_COMMAND_ENV,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    if process.stdout is None or process.stderr is None:
        raise RuntimeError("worker command pipes were not created")
    readers = asyncio.gather(
        _read_bounded(process.stdout), _read_bounded(process.stderr)
    )
    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), timeout_seconds)
    except TimeoutError:
        timed_out = True
    # The whole group dies either way: a finished shell may leave background
    # children holding the pipes open, which would stall the readers.
    _kill_group(process.pid)
    await process.wait()
    try:
        (out, out_cut), (err, err_cut) = await asyncio.wait_for(
            readers, _DRAIN_GRACE_SECONDS
        )
    except TimeoutError:
        readers.cancel()
        return _failure(job.job_id, "output_drain_timeout")
    return WorkerResult(
        job_id=job.job_id,
        exit_code=None if timed_out else process.returncode,
        stdout=_decode_bounded(out),
        stderr=_decode_bounded(err),
        truncated=out_cut or err_cut,
        reason_code="command_timeout" if timed_out else None,
    )


def _file_result(job_id: UUID, output: FileOutput) -> WorkerResult:
    text = _decode_bounded(output.text.encode("utf-8"))
    return WorkerResult(
        job_id=job_id,
        exit_code=0,
        stdout=text,
        stderr="",
        truncated=output.truncated or text != output.text,
        reason_code=None,
    )


def _write(workspace: Path, payload: WriteFilePayload) -> FileOutput:
    write_file(workspace, payload.path, payload.content)
    return FileOutput("written")


def _file_operation(job: WorkerJob, workspace: Path) -> FileOutput | None:
    """Run a non-command operation synchronously; None when it is not one."""
    payload = job.payload
    output: FileOutput | None = None
    match payload:
        case ReadFilePayload():
            output = read_file(workspace, payload.path, payload.offset)
        case ListFilesPayload():
            output = list_files(workspace, payload.path)
        case SearchTextPayload():
            output = search_text(workspace, payload.pattern, payload.path)
        case WriteFilePayload():
            output = _write(workspace, payload)
        case ApplyPatchPayload():
            output = apply_patch(workspace, payload.patch)
        case ExportPatchPayload():
            output = export_patch(workspace)
    return output


async def execute_job(
    job: WorkerJob, workspace: Path, timeout_seconds: float
) -> WorkerResult:
    """Run one validated job. Every refusal is a structured reason code."""
    payload = job.payload
    if isinstance(payload, RunCommandPayload):
        return await _run_command(job, payload.command, workspace, timeout_seconds)
    try:
        output = await asyncio.to_thread(_file_operation, job, workspace)
    except WorkspaceFileError as error:
        return _failure(job.job_id, error.reason_code)
    if output is None:
        return _failure(job.job_id, "operation_unsupported")
    return _file_result(job.job_id, output)


async def run_stdio(
    stdin: TextIO, stdout: TextIO, workspace: Path, timeout_seconds: float
) -> None:
    """Read one job line, execute it, write one result line."""
    line = stdin.readline(MAX_FRAME_BYTES + 1)
    try:
        job = WorkerJob.model_validate_json(line)
    except ValidationError:
        stdout.write(_failure(_NIL_JOB, "job_invalid").model_dump_json())
        stdout.write("\n")
        stdout.flush()
        return
    result = await execute_job(job, workspace, timeout_seconds)
    stdout.write(result.model_dump_json())
    stdout.write("\n")
    stdout.flush()


def _import_failure(reason_code: str) -> ImportOutcome:
    return ImportOutcome(ok=False, reason_code=reason_code)


def run_import(stdin: BinaryIO, stdout: TextIO, workspace: Path) -> None:
    """Import mode: an options line, then a tar stream, into the empty workspace."""
    try:
        options = ImportOptions.model_validate_json(stdin.readline(MAX_FRAME_BYTES + 1))
    except ValidationError:
        outcome = _import_failure("import_options_invalid")
    else:
        outcome = _admit_and_baseline(stdin, workspace, options)
    stdout.write(outcome.model_dump_json())
    stdout.write("\n")
    stdout.flush()


def _admit_and_baseline(
    stream: BinaryIO, workspace: Path, options: ImportOptions
) -> ImportOutcome:
    try:
        report = admit_archive(
            stream,
            workspace,
            extra_deny_globs=options.extra_deny_globs,
            allowed_findings=frozenset(options.allowed_findings),
        )
        init_baseline(workspace)
    except AdmissionRefused as refused:
        return ImportOutcome(
            ok=False,
            reason_code=refused.reason_code,
            findings=tuple(
                ImportFinding(path=f.path, rule=f.rule)
                for f in refused.findings[:MAX_IMPORT_REPORT_ITEMS]
            ),
        )
    except WorkspaceFileError as error:
        return _import_failure(error.reason_code)
    except ValueError:
        return _import_failure("import_options_invalid")
    return ImportOutcome(
        ok=True,
        files=report.files,
        directories=report.directories,
        total_bytes=report.total_bytes,
        excluded=tuple(report.excluded),
    )


def main() -> None:
    if sys.argv[1:] == [IMPORT_MODE_ARGUMENT]:
        run_import(sys.stdin.buffer, sys.stdout, WORKSPACE)
        return
    asyncio.run(run_stdio(sys.stdin, sys.stdout, WORKSPACE, COMMAND_TIMEOUT_SECONDS))


if __name__ == "__main__":
    main()
