"""Runs the real service stack inside the built service image (test driver only).

The official CLI, the tool gateway, the stdio bridge and ``ClaudeService`` are all
the production code. Only two things are substituted, both outside any security
boundary: the subscription inspector (no login exists in a synthetic run) and the
CLI environment, which gains the synthetic provider's address and a throwaway key
so the CLI talks to the fake provider on the internal test network. The worker
executor records the jobs it receives instead of starting containers.

stdin: JSON {"base_url", "operations", "purpose", "run_seconds"}. stdout: one JSON
summary line. It never prints environment values.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from roboco.runtime.subscription import policy, service
from roboco.runtime.subscription.contracts import (
    RunRequest,
    RunSpec,
    SubscriptionState,
    SubscriptionStatus,
    ToolManifest,
    WorkerJob,
    WorkerOperation,
    WorkerResult,
)
from roboco.runtime.subscription.gateway_socket import GatewayServer
from roboco.runtime.subscription.tool_gateway import ToolGateway

SENTINEL = "synthetic-credential-sentinel-9d2c41"


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

    def synthetic_env() -> dict[str, str]:
        return {
            **real_env(),
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
    claude = service.ClaudeService(
        gateway=gateway,
        runtime_dir=Path("/run/roboco/runs"),
        socket_path=socket_path,
        inspect=_ready,
        run_seconds=options.get("run_seconds", 120),
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
        operations=tuple(WorkerOperation(name) for name in options["operations"])
    )
    result = await claude.run(
        RunRequest(spec=spec, manifest=manifest if delivery else None)
    )
    await server.stop()
    summary = {
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
