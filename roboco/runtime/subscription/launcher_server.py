"""Fixed-profile worker launcher: the only component that talks to Docker.

It is deliberately not a Docker proxy. A request is one validated frame; the only
thing it can start is the configured worker image under the fixed security
profile, with one registered workspace volume. Project code never runs here.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from pydantic import ValidationError

from roboco.runtime.subscription.contracts import WorkerResult
from roboco.runtime.subscription.launcher_policy import (
    RUN_LABEL,
    WORKER_LABEL,
    ClientAuthenticator,
    JobRejection,
    LauncherConfig,
    LauncherRegistry,
    build_worker_run_argv,
)
from roboco.runtime.subscription.launcher_protocol import (
    MAX_FRAME_BYTES,
    MAX_RESPONSE_BYTES,
    CancelRunFrame,
    ExecuteFrame,
    HealthFrame,
    LauncherResponse,
    ProtocolError,
    RegisterRunFrame,
    RegisterWorkspaceFrame,
    encode_response,
    parse_frame,
)

if TYPE_CHECKING:
    from uuid import UUID

    from roboco.runtime.subscription.launcher_protocol import LauncherFrame

logger = logging.getLogger("roboco.launcher")

DEFAULT_MAX_CONCURRENT = 2
DEFAULT_MAX_PENDING = 16
RUN_TTL = timedelta(minutes=45)
_FRAME_READ_TIMEOUT_SECONDS = 10.0
_DOCKER_CLI_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class WorkerRun:
    returncode: int | None
    stdout: bytes
    timed_out: bool


class DockerAdapter(Protocol):
    async def run_worker(
        self, argv: list[str], stdin: bytes, timeout_seconds: float
    ) -> WorkerRun: ...

    async def kill_containers(self, names: list[str]) -> None: ...

    async def list_owned_containers(self, run_id: UUID | None = None) -> list[str]: ...


class DockerCli:
    """The real adapter: argv-list subprocess calls to the docker CLI, no shell."""

    async def run_worker(
        self, argv: list[str], stdin: bytes, timeout_seconds: float
    ) -> WorkerRun:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            returncode, stdout = await asyncio.wait_for(
                self._feed_and_collect(process, stdin), timeout_seconds
            )
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            return WorkerRun(returncode=None, stdout=b"", timed_out=True)
        return WorkerRun(returncode=returncode, stdout=stdout, timed_out=False)

    @staticmethod
    async def _feed_and_collect(
        process: asyncio.subprocess.Process, stdin: bytes
    ) -> tuple[int, bytes]:
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("worker process pipes were not created")
        process.stdin.write(stdin)
        await process.stdin.drain()
        process.stdin.close()
        collected = bytearray()
        while chunk := await process.stdout.read(64 * 1024):
            collected.extend(chunk)
            if len(collected) > MAX_RESPONSE_BYTES:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                break
        return await process.wait(), bytes(collected)

    async def kill_containers(self, names: list[str]) -> None:
        if names:
            await self._docker(["kill", *names])

    async def list_owned_containers(self, run_id: UUID | None = None) -> list[str]:
        filters = ["--filter", f"label={WORKER_LABEL}=1"]
        if run_id is not None:
            filters += ["--filter", f"label={RUN_LABEL}={run_id}"]
        output = await self._docker(["ps", "--quiet", "--no-trunc", *filters])
        return output.split()

    @staticmethod
    async def _docker(args: list[str]) -> str:
        process = await asyncio.create_subprocess_exec(
            "docker",
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(), _DOCKER_CLI_TIMEOUT_SECONDS
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            return ""
        return stdout.decode("utf-8", errors="replace")


class Launcher:
    def __init__(  # noqa: PLR0913 - explicit trusted wiring
        self,
        *,
        config: LauncherConfig,
        registry: LauncherRegistry,
        auth: ClientAuthenticator,
        docker: DockerAdapter,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        max_pending: int = DEFAULT_MAX_PENDING,
        hard_timeout_seconds: float | None = None,
    ) -> None:
        self._config = config
        self._registry = registry
        self._auth = auth
        self._docker = docker
        self._slots = asyncio.Semaphore(max_concurrent)
        self._capacity = max_concurrent + max_pending
        self._outstanding = 0
        self._hard_timeout = hard_timeout_seconds or (
            config.command_timeout_seconds + config.hard_kill_grace_seconds
        )
        self._inflight: dict[UUID, set[str]] = {}

    async def handle_frame(self, raw: bytes) -> LauncherResponse:
        try:
            frame = parse_frame(raw)
        except ProtocolError as exc:
            return LauncherResponse(ok=False, reason_code=str(exc))
        if not self._auth.allows(frame.token, frame.action):
            logger.warning("Launcher request refused: unauthorized action")
            return LauncherResponse(ok=False, reason_code="unauthorized")
        try:
            return await self._dispatch(frame)
        except JobRejection as rejection:
            logger.info("Launcher rejected request: %s", rejection.reason_code)
            return LauncherResponse(ok=False, reason_code=rejection.reason_code)

    async def _dispatch(self, frame: LauncherFrame) -> LauncherResponse:
        now = datetime.now(UTC)
        if isinstance(frame, HealthFrame):
            return LauncherResponse(ok=True)
        if isinstance(frame, RegisterWorkspaceFrame):
            self._registry.register_workspace(
                frame.body.workspace_handle, frame.body.project_id
            )
            return LauncherResponse(ok=True)
        if isinstance(frame, RegisterRunFrame):
            self._registry.register_run(
                frame.body.run_id,
                frame.body.project_id,
                frame.body.workspace_handle,
                now,
            )
            return LauncherResponse(ok=True)
        if isinstance(frame, CancelRunFrame):
            await self._cancel(frame.body.run_id)
            return LauncherResponse(ok=True)
        if not isinstance(frame, ExecuteFrame):
            raise JobRejection("frame_invalid")
        return await self._execute(frame, now)

    async def _cancel(self, run_id: UUID) -> None:
        self._registry.cancel_run(run_id)
        names = set(self._inflight.get(run_id, set()))
        names.update(await self._docker.list_owned_containers(run_id))
        await self._docker.kill_containers(sorted(names))
        logger.info("Run cancelled: %s (%d containers)", run_id, len(names))

    async def _execute(self, frame: ExecuteFrame, now: datetime) -> LauncherResponse:
        if self._outstanding >= self._capacity:
            return LauncherResponse(ok=False, reason_code="queue_full")
        authorized = self._registry.authorize(frame.body.job, now)
        job = frame.body.job
        self._outstanding += 1
        try:
            async with self._slots:
                self._inflight.setdefault(job.run_id, set()).add(
                    authorized.container_name
                )
                try:
                    run = await self._docker.run_worker(
                        build_worker_run_argv(self._config, authorized),
                        job.model_dump_json().encode() + b"\n",
                        self._hard_timeout,
                    )
                finally:
                    self._inflight[job.run_id].discard(authorized.container_name)
        finally:
            self._outstanding -= 1
        if run.timed_out:
            await self._docker.kill_containers([authorized.container_name])
            return LauncherResponse(ok=False, reason_code="worker_timeout")
        return self._response_from_worker(run, job.job_id, job.run_id)

    def _response_from_worker(
        self, run: WorkerRun, job_id: UUID, run_id: UUID
    ) -> LauncherResponse:
        if self._registry.is_cancelled(run_id):
            return LauncherResponse(ok=False, reason_code="run_cancelled")
        try:
            result = WorkerResult.model_validate_json(run.stdout)
        except ValidationError:
            return LauncherResponse(ok=False, reason_code="worker_result_invalid")
        if result.job_id != job_id:
            return LauncherResponse(ok=False, reason_code="worker_result_invalid")
        return LauncherResponse(ok=True, result=result)

    async def reconcile_orphans(self, *, dry_run: bool) -> list[str]:
        """Find (and unless dry_run, kill) worker containers this launcher owns."""
        owned = await self._docker.list_owned_containers()
        if owned and not dry_run:
            await self._docker.kill_containers(owned)
        logger.info(
            "Reconcile: %d owned worker containers (dry_run=%s)", len(owned), dry_run
        )
        return owned

    async def serve(self, host: str, port: int) -> None:
        server = await asyncio.start_server(
            self._handle_connection, host, port, limit=MAX_FRAME_BYTES + 1
        )
        logger.info("Launcher listening on %s:%d", host, port)
        async with server:
            await server.serve_forever()

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            try:
                raw = await asyncio.wait_for(
                    reader.readuntil(b"\n"), _FRAME_READ_TIMEOUT_SECONDS
                )
                response = await self.handle_frame(raw.rstrip(b"\n"))
            except (
                asyncio.LimitOverrunError,
                asyncio.IncompleteReadError,
                TimeoutError,
            ):
                response = LauncherResponse(ok=False, reason_code="frame_unreadable")
            writer.write(encode_response(response))
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()
