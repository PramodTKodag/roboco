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
from typing import TYPE_CHECKING, TextIO
from uuid import UUID

from pydantic import ValidationError

from roboco.runtime.subscription.contracts import (
    COMMAND_TIMEOUT_SECONDS,
    MAX_STREAM_BYTES,
    RunCommandPayload,
    WorkerJob,
    WorkerOperation,
    WorkerResult,
)
from roboco.runtime.subscription.launcher_protocol import MAX_FRAME_BYTES

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

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


async def execute_job(
    job: WorkerJob, workspace: Path, timeout_seconds: float
) -> WorkerResult:
    """Run one validated job. Unsupported operations fail explicitly."""
    handlers: dict[WorkerOperation, Callable[[], Awaitable[WorkerResult]]] = {}
    payload = job.payload
    if isinstance(payload, RunCommandPayload):
        command = payload.command
        handlers[WorkerOperation.RUN_COMMAND] = lambda: _run_command(
            job, command, workspace, timeout_seconds
        )
    handler = handlers.get(job.operation)
    if handler is None:
        return _failure(job.job_id, "operation_unsupported")
    return await handler()


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


def main() -> None:
    asyncio.run(run_stdio(sys.stdin, sys.stdout, WORKSPACE, COMMAND_TIMEOUT_SECONDS))


if __name__ == "__main__":
    main()
