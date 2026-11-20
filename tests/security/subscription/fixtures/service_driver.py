"""Runs the real service stack inside the built service image (test driver only).

The official CLI, the tool gateway, the stdio bridge and ``ClaudeService`` are all
the production code. Only two things are substituted, both outside any security
boundary: the subscription inspector (no login exists in a synthetic run) and the
CLI environment, which gains the synthetic provider's address and a throwaway key
so the CLI talks to the fake provider on the internal test network. The worker
executor records the jobs it receives instead of starting containers.

A verb run also starts a loopback stand-in for the controller API that records
the method, path and header names of every verb call (never header values other
than a match flag), so the tests can show what a verb server sends and to where.

stdin: JSON {"base_url", "operations", "purpose", "run_seconds", "verbs"}.
``verbs`` is a list of {"server", "verbs"} grants. stdout: one JSON summary line.
It never prints environment values.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

from roboco.runtime.subscription import policy, service
from roboco.runtime.subscription.contracts import (
    RunRequest,
    RunSpec,
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
from roboco.runtime.subscription.gateway_socket import GatewayServer
from roboco.runtime.subscription.tool_gateway import ToolGateway

SENTINEL = "synthetic-credential-sentinel-9d2c41"
RUN_TOKEN = "synthetic-run-token-0123456789abcdef"
SESSION_ID = "synthetic-chat-session-01"
_ENVELOPE = {"status": "ok", "task_id": None, "next": "synthetic"}


class RecordingApi:
    """A loopback stand-in for the controller API; records, never judges."""

    def __init__(self, agent_id: str) -> None:
        self.requests: list[dict[str, Any]] = []
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def _record(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length).decode() if length else ""
                recorder.requests.append(
                    {
                        "method": self.command,
                        "path": self.path.split("?")[0],
                        "header_names": sorted(k.lower() for k in self.headers),
                        "token_matches": self.headers.get("x-agent-token") == RUN_TOKEN,
                        "agent_matches": self.headers.get("x-agent-id") == agent_id,
                        "sentinel_in_request": SENTINEL in body
                        or any(SENTINEL in v for v in self.headers.values()),
                    }
                )
                payload = json.dumps(_ENVELOPE).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _record

            def log_message(self, *_: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class RecordingExecutor:
    def __init__(self) -> None:
        self.jobs: list[WorkerJob] = []

    async def execute(self, job: WorkerJob) -> WorkerResult:
        self.jobs.append(job)
        return WorkerResult(
            job_id=job.job_id,
            exit_code=0,
            stdout="worker-output",
            stderr="",
            truncated=False,
            reason_code=None,
        )


async def _ready(**_: object) -> SubscriptionStatus:
    return SubscriptionStatus(
        state=SubscriptionState.READY,
        authenticated=True,
        auth_method="claude.ai",
        checked_at=datetime.now(UTC),
        reason_code=None,
    )


async def main() -> None:
    options = json.loads(sys.stdin.read())
    real_env = policy.build_claude_env

    def synthetic_env(egress: policy.EgressRoute | None = None) -> dict[str, str]:
        return {
            **real_env(egress),
            "ANTHROPIC_BASE_URL": options["base_url"],
            "ANTHROPIC_API_KEY": "synthetic-transport-key",
        }

    service.build_claude_env = synthetic_env
    credentials = Path(policy.CLAUDE_CONFIG_DIR) / ".credentials.json"
    credentials.parent.mkdir(parents=True, exist_ok=True)
    credentials.write_text(json.dumps({"synthetic": SENTINEL}))

    executor = RecordingExecutor()
    gateway = ToolGateway(executor)
    socket_path = Path("/run/roboco/gateway/gateway.sock")
    server = GatewayServer(gateway)
    await server.start(socket_path)
    agent_id = uuid4()
    api = RecordingApi(str(agent_id))
    grants = tuple(
        VerbGrant(server=VerbServer(grant["server"]), verbs=tuple(grant["verbs"]))
        for grant in options.get("verbs", [])
    )
    claude = service.ClaudeService(
        gateway=gateway,
        runtime_dir=Path("/run/roboco/runs"),
        socket_path=socket_path,
        inspect=_ready,
        run_seconds=options.get("run_seconds", 120),
        orchestrator_url=api.url if grants else "",
    )
    delivery = options["purpose"] == "delivery"
    spec = RunSpec(
        run_id=uuid4(),
        project_id=uuid4() if delivery else None,
        purpose=options["purpose"],
        model="claude-sonnet-5-5",
        prompt="Use your tools.",
        workspace_handle=uuid4() if delivery else None,
    )
    manifest = ToolManifest(
        operations=tuple(WorkerOperation(name) for name in options["operations"]),
        verb_grants=grants,
    )
    wants_session = any(g.server is VerbServer.INTAKE for g in grants)
    result = await claude.run(
        RunRequest(
            spec=spec,
            manifest=manifest if (delivery or grants) else None,
            verb_credentials=(
                VerbCredentials(
                    agent_id=agent_id, role=options["role"], token=RUN_TOKEN
                )
                if grants
                else None
            ),
            session_id=SESSION_ID if wants_session else None,
        )
    )
    await server.stop()
    api.stop()
    summary = {
        "api_requests": api.requests,
        "state": result.state.value,
        "reason": result.reason_code,
        "text": result.text,
        "jobs": [
            {
                "operation": job.operation.value,
                "run_matches": job.run_id == spec.run_id,
                "project_matches": job.project_id == spec.project_id,
                "workspace_matches": job.workspace_handle == spec.workspace_handle,
                "payload": job.payload.model_dump(mode="json"),
            }
            for job in executor.jobs
        ],
        "empty_cwd_listing": sorted(p.name for p in service.RUN_CWD.iterdir()),
    }
    sys.stdout.write(json.dumps(summary) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
