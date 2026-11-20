"""The trusted Claude service: the only component that runs the official CLI.

One credential profile means one run at a time. Auth inspection and model runs
share a single lock, so a status check never races a token refresh. A run starts
the CLI with the single argv builder, a clean environment, a fixed empty working
directory and the prompt on stdin; its stderr is discarded and its stream is
reduced to a bounded outcome. Cancellation and expiry kill the whole process
group and revoke the run's tool capability.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from roboco.runtime.subscription.auth import inspect_subscription
from roboco.runtime.subscription.cli import (
    CLAUDE_BINARY,
    RUN_CWD,
    build_claude_argv,
    build_mcp_config,
    build_settings,
    expected_tool_names,
)
from roboco.runtime.subscription.contracts import (
    MAX_QUEUED_RUNS,
    MAX_RUN_SECONDS,
    RunRequest,
    RunResult,
    RunState,
    SubscriptionState,
)
from roboco.runtime.subscription.policy import build_claude_env
from roboco.runtime.subscription.stream_parser import Outcome, StreamParser
from roboco.runtime.subscription.tool_gateway import ToolDenied, ToolGateway

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from uuid import UUID

    from roboco.runtime.subscription.contracts import SubscriptionStatus

logger = logging.getLogger(__name__)

MAX_STREAM_LINE_BYTES = 4 * 1024 * 1024
_DIR_MODE = 0o700
_FILE_MODE = 0o600
_STATE_FOR_STATUS = {
    SubscriptionState.LOGIN_REQUIRED: RunState.LOGIN_REQUIRED,
    SubscriptionState.QUOTA_EXHAUSTED: RunState.QUOTA_EXHAUSTED,
    SubscriptionState.POLICY_BLOCKED: RunState.BLOCKED,
    SubscriptionState.CONFIGURATION_ERROR: RunState.FAILED,
}

type StatusInspector = Callable[..., Awaitable[SubscriptionStatus]]


@dataclass
class _ActiveRun:
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    process: asyncio.subprocess.Process | None = None


def _kill_group(process: asyncio.subprocess.Process) -> None:
    """SIGKILL the CLI's own session group.

    The group is ours (``start_new_session``), so the only failures are an empty
    group (``ProcessLookupError``) or, on macOS, a group whose members are all
    unreaped zombies (``PermissionError``); both mean nothing is left to kill.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)


def _write_private(path: Path, content: dict[str, object]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(content, handle)


class ClaudeService:
    def __init__(  # noqa: PLR0913 - explicit trusted wiring
        self,
        *,
        gateway: ToolGateway,
        runtime_dir: Path,
        socket_path: Path,
        cli_path: Path = CLAUDE_BINARY,
        python: Path | None = None,
        cwd: Path = RUN_CWD,
        run_seconds: float = MAX_RUN_SECONDS,
        max_queued: int = MAX_QUEUED_RUNS,
        inspect: StatusInspector = inspect_subscription,
    ) -> None:
        self._gateway = gateway
        self._runtime_dir = runtime_dir
        self._socket_path = socket_path
        self._cli_path = cli_path
        self._python = python or Path(sys.executable)
        self._cwd = cwd
        self._run_seconds = run_seconds
        self._max_queued = max_queued
        self._inspect = inspect
        self._credential_lock = asyncio.Lock()
        self._waiting = 0
        self._active: dict[UUID, _ActiveRun] = {}

    async def status(self) -> SubscriptionStatus:
        async with self._credential_lock:
            return await self._inspect(cli_path=self._cli_path)

    async def cancel(self, run_id: UUID) -> bool:
        """Stop a queued or running run and revoke its tools; False if unknown."""
        active = self._active.get(run_id)
        self._gateway.revoke(run_id)
        if active is None:
            return False
        active.cancelled.set()
        if active.process is not None:
            _kill_group(active.process)
        logger.info("Run cancelled: run=%s", run_id)
        return True

    async def run(self, request: RunRequest) -> RunResult:
        run_id = request.spec.run_id
        refusal = self._refusal_before_queue(request)
        if refusal is not None:
            return _result(run_id, RunState.BLOCKED, refusal)
        active = _ActiveRun()
        self._active[run_id] = active
        self._waiting += 1
        try:
            async with self._credential_lock:
                self._waiting -= 1
                return await self._run_locked(request, active)
        finally:
            self._active.pop(run_id, None)
            self._gateway.revoke(run_id)

    def _refusal_before_queue(self, request: RunRequest) -> str | None:
        if request.spec.run_id in self._active:
            return "run_already_active"
        if self._waiting >= self._max_queued:
            return "queue_full"
        try:
            self._gateway.validate(request)
        except ToolDenied as denied:
            return denied.reason_code
        return None

    async def _run_locked(self, request: RunRequest, active: _ActiveRun) -> RunResult:
        run_id = request.spec.run_id
        if active.cancelled.is_set():
            return _result(run_id, RunState.CANCELLED, "cancelled")
        status = await self._inspect(cli_path=self._cli_path)
        if status.state is not SubscriptionState.READY:
            logger.warning("Run refused: run=%s subscription=%s", run_id, status.state)
            return _result(
                run_id,
                _STATE_FOR_STATUS[status.state],
                status.reason_code or status.state.value,
            )
        try:
            capability = self._gateway.bind(request)
        except ToolDenied as denied:
            return _result(run_id, RunState.BLOCKED, denied.reason_code)
        run_dir = self._runtime_dir / run_id.hex
        try:
            run_dir.mkdir(mode=_DIR_MODE, parents=True)
            outcome = await self._execute(request, active, run_dir, capability)
        except OSError:
            logger.exception("Run could not start: run=%s", run_id)
            outcome = Outcome(RunState.FAILED, "cli_unavailable", "", {})
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)
        logger.info(
            "Run finished: run=%s state=%s reason=%s",
            run_id,
            outcome.state.value,
            outcome.reason_code,
        )
        return RunResult(
            run_id=run_id,
            state=outcome.state,
            text=outcome.text,
            reason_code=outcome.reason_code,
            usage=outcome.usage,
        )

    async def _execute(
        self,
        request: RunRequest,
        active: _ActiveRun,
        run_dir: Path,
        capability: str | None,
    ) -> Outcome:
        operations = (
            request.manifest.operations if capability and request.manifest else ()
        )
        settings_file = run_dir / "settings.json"
        _write_private(settings_file, build_settings(operations))
        mcp_config: Path | None = None
        if capability is not None:
            mcp_config = run_dir / "mcp.json"
            _write_private(
                mcp_config,
                build_mcp_config(
                    python=self._python,
                    socket_path=self._socket_path,
                    capability=capability,
                ),
            )
        argv = build_claude_argv(
            request.spec, mcp_config=mcp_config, settings_file=settings_file
        )
        process = await asyncio.create_subprocess_exec(
            str(self._cli_path),
            *argv,
            env=build_claude_env(),
            cwd=self._cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            limit=MAX_STREAM_LINE_BYTES,
        )
        active.process = process
        parser = StreamParser(expected_tools=expected_tool_names(operations))
        try:
            await self._send_prompt(process, request.spec.prompt)
            return await self._pump(process, parser, active)
        finally:
            _kill_group(process)
            await process.wait()

    @staticmethod
    async def _send_prompt(process: asyncio.subprocess.Process, prompt: str) -> None:
        assert process.stdin is not None  # nosec B101 - PIPE requested above
        try:
            process.stdin.write(prompt.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            logger.warning("CLI closed stdin before the prompt was written")

    async def _pump(
        self,
        process: asyncio.subprocess.Process,
        parser: StreamParser,
        active: _ActiveRun,
    ) -> Outcome:
        assert process.stdout is not None  # nosec B101 - PIPE requested above
        try:
            async with asyncio.timeout(self._run_seconds):
                async for line in process.stdout:
                    violation = parser.feed(line)
                    if violation is not None:
                        _kill_group(process)
                        return parser.outcome()
        except TimeoutError:
            return parser.partial_outcome(RunState.BLOCKED, "run_time_limit")
        except (asyncio.LimitOverrunError, ValueError):
            return parser.partial_outcome(RunState.FAILED, "stream_line_too_large")
        if active.cancelled.is_set():
            return parser.partial_outcome(RunState.CANCELLED, "cancelled")
        return parser.outcome()


def _result(run_id: UUID, state: RunState, reason_code: str) -> RunResult:
    return RunResult(
        run_id=run_id, state=state, text="", reason_code=reason_code, usage={}
    )
