---
paths:
  - "roboco/runtime/subscription/**"
  - "docker/claude-subscription.Dockerfile"
  - "docker/task-worker.Dockerfile"
  - "tests/security/subscription/**"
---

# Subscription runtime: Claude service, tool gateway, offline workers

Model work runs only through the official CLI logged in with the owner's subscription. Credentials live in one small trusted image (`docker/claude-subscription.Dockerfile`); project code runs only in the credential-free, network-less worker (`docker/task-worker.Dockerfile`).

- `cli.py` is the only place that builds the CLI argv, MCP config and settings. Never add `--bare`, `bypassPermissions` or `auto`; never pass model, task or project text into argv, env or config (the prompt goes over stdin). The CLI environment is the allowlist in `policy.build_claude_env()`, including `CLAUDE_CODE_MAX_RETRIES`.
- Model tools exist only as worker operations granted by a run's `ToolManifest`. Authorization comes from the per-run capability bound by `ToolGateway.bind()` from the controller-registered run; arguments from the model can never name a run, project, workspace or operation. A missing manifest means zero tools.
- `StreamParser` requires the init event's tool list to equal the granted set exactly. Do not relax it to a subset check.
- The service never persists or logs raw CLI events or vendor error text; outcomes carry fixed reason codes. Auth and quota failures surface as `login_required` / `quota_exhausted` and never trigger a fallback.
- The bridge runs in the CLI's minimal environment, so its import root travels in its MCP env; a new bridge dependency must also be added to `MCP_PACKAGES` in `scripts/personal/export_worker_requirements.py` and the image rebuilt (the container tests catch a miss).
- The service image copies only its import closure; `tests/unit/subscription/test_service_image_contents.py` enforces it. Run the container suite with `tests/security/subscription/test_runtime_boundary.py` (needs the staged CLI from `scripts/personal/probe_cli_contract.py`).
