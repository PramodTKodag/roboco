# Personal RoboCo Fork: Claude Subscription and Credential Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL, if installed: use `superpowers:executing-plans` to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking. The owner intends a separate implementation session; this document authorizes planning only in the current session. Follow the execution handoff in §17 when the owner supplies this plan to the implementation session. Do not require this conversation or audit's temporary files.

**Goal:** Build a personal RoboCo fork whose model work uses the owner's Claude subscription exclusively, with no API keys or provider fallback, while keeping provider credentials inaccessible to the model's tools and all project-controlled code.

**Architecture:** Keep the official Claude Code executable in a small trusted service, with its own dedicated login store and no project mounts or built-in execution/file tools. All model-requested project operations go through a role- and run-scoped tool gateway into separate disposable containers that have neither credentials nor network access. A fixed-profile launcher and an independently enforced egress policy establish the security boundary; prompts and shell-deny regexes do not.

**Tech stack:** Existing Python 3.13+ application, FastAPI, PostgreSQL, Redis, Next.js panel, Docker Compose/Docker Desktop on the owner's Mac, official Claude Code CLI, existing MCP flow/do interfaces. Reuse the repository's existing tools and tests; do not replace its task system or add another orchestration framework.

**Spec:** The embedded design and requirements in §§1–8 of this document. This is the complete handoff; no separate design document is required.

**Personal fork:** <https://github.com/PramodTKodag/roboco>

**Local checkout:** `/Users/thesilentknight/Projects/Personal/roboco`

**Hardening branch:** `docs/security/claude-subscription-hardening`

**Plan path:** `docs/personal/claude-subscription-hardening-plan.md`

**Remotes:** `origin` is `https://github.com/PramodTKodag/roboco.git`; `upstream` is `https://github.com/rennf93/roboco.git`. Default push remote is `origin`.

**Status:** Personal fork and hardening branch are prepared. This is the implementation specification; product hardening has not started. No credentials, live subscription calls, container builds, or firewall changes were made while writing/publishing this plan. The runtime/auth/network proof in Task 1 is a required feasibility gate, not an already validated result.

## 1. Authority, scope, and personal-project boundaries

- This is the owner's **personal project**, not FabricBloc or a company platform change. Do not create files, branches, issues, deployments, or secrets in a company repository or organization.
- Upstream: <https://github.com/rennf93/roboco>. Audited source baseline: `9d7747f65050f70489b26ec56b9df2d593bf4d87`, confirmed as upstream master on 2026-11-20. Recheck upstream before implementing. Report drift and map changed callers; do not blindly apply old line numbers.
- Implementation checkout already exists at `/Users/thesilentknight/Projects/Personal/roboco`, with personal fork `PramodTKodag/roboco` as `origin` and original repository `rennf93/roboco` as `upstream`. Continue on `docs/security/claude-subscription-hardening`; do not recreate the fork, clone over this directory, reset it, or replace its remotes. Verify existing state before editing.
- Preserve the actual upstream AGPL-3.0 license and human copyright notices. Do not add assistant attribution. This plan does not authorize relicensing.
- Read the fork's `AGENTS.md`, `pyproject.toml`, Makefile, and relevant `.claude/rules/` files. Their comments are context; verify behavior in code. Do not load unrelated company workflows or company secrets.
- The accepted scope is **Claude subscription only**. Codex, Cursor, Hummin, Grok, Gemini, OpenRouter, Nebius, cloud Ollama, and third-party model APIs are excluded from this fork's supported runtime. Do not add the earlier-discussed multi-provider feature.
- Existing working personal Claude installations and Keychain entries must not be copied, edited, logged out, or deleted. Create a separate runtime profile through supported Claude authentication.
- Do not run upstream cleanup/reset/install targets without reading them. Several Makefile targets recursively delete caches; tests may destroy databases. Use disposable test namespaces and explicit per-scope approval for destructive actions affecting anything else.

## 2. Exact interpretation of the security goal

### 2.1 What is protected

Provider access/refresh tokens, API keys accidentally present on the host, macOS Keychain data, SSH agents/keys, Git tokens, cloud credentials, controller database passwords, signing keys, personal Claude history/configuration, other projects, and the host Docker control interface must never enter a project worker or a model/tool payload.

The authenticated official Claude process necessarily needs access to its own subscription credential. Therefore “isolated from agents” means **isolated from model-visible tools, untrusted project processes, project files, their environment, and the control-panel API**, not that the official authentication client can function without a credential. Treat the CLI service, credential volume, launcher, and egress proxy as a small trusted computing base. The model must have no capability that reads their files or launches processes there.

The selected source files and tool results needed for a task will be sent to Anthropic. The plan cannot promise that code analyzed by a hosted model stays on the owner's computer. Nor can a secret scanner prove that an arbitrary source file contains no secret. Only an owner-approved, sanitized project snapshot is admitted; hard exclusion and namespace boundaries are the main controls.

### 2.2 Model and billing policy

- Official Claude subscription login only; accept verified first-party `claude.ai` authentication. Reject Console/API-key, helper-key, external-gateway, Bedrock, Vertex, Foundry, and unknown auth modes.
- Do not use API keys even if bundled monthly API credits are available with a subscription.
- No alternate providers, alternate auth methods, automatic account switching, cloud-model shortcuts, or unrequested model substitution. A configured model that cannot run must fail visibly.
- A missing/expired/revoked session, quota exhaustion, unsupported model, malformed stream, or failed auth check must stop or park work. Never silently claim completion or skip required validation.
- Network timeouts may be retried on the **same run configuration and same model**, at most twice with bounded backoff; do not retry authentication/quota errors in a loop. Tool writes need idempotency and cannot be blindly repeated after an uncertain result.
- Default maximum concurrent Claude subprocesses per credential profile: **1**, including Intake, Secretary, delivery roles, and background jobs. Queue requests; interactive work may take priority without starving older work. This avoids concurrent refresh races and accidental fleet-wide spending.
- “Subscription login” alone does not prevent separately billed extra/usage credits. The owner must disable extra usage/usage credits and automatic top-ups for the account used by the dedicated runtime profile, as applicable in Claude's account settings. A new CLI profile does not create separate billing: these account-level changes can also affect the owner's other Claude sessions and must be performed by the owner, not automated. The software must not enable them, request payment, or treat a local assertion as proof of account billing state. See the setup gate in Task 2.

### 2.3 Network and workflow policy

- During a normal task, the only internet traffic from the application stack is the official Claude runtime's essential, reviewed Anthropic authentication/inference traffic through the egress proxy. No developer-owned destinations, analytics collectors, web search, external images, arbitrary web fetch, Git forge traffic, package downloads, or browser automation.
- Authentication is a distinct owner-operated maintenance action, not a model tool. It may temporarily need additional documented first-party login endpoints; normal runs must not inherit maintenance permissions.
- Builds/dependency acquisition occur in a separate maintenance environment with **no provider credentials, no private task data, and no mounted project checkout**. Fetch locked dependencies first; run project lifecycle scripts only later inside offline workers. If a task needs an unavailable package, return `dependency_unavailable`; do not grant internet or fall back to installing in the controller.
- Disable automatic GitHub/GitLab/Gitea clone/push/PR operations in this strict profile. Import a sanitized local snapshot and export a patch/local Git bundle for the owner. External forge integration would require a future, explicitly different egress policy and credential boundary.
- No fake PRs or dummy URLs to satisfy upstream lifecycle guards. Implement an explicit personal/local review mode in Task 9.

### 2.4 Threat assumptions and limits

Defend against malicious repository contents, package install/build/test scripts, prompt injection, compromised task outputs, model-generated shell, path traversal/symlink races, cross-run/project access, missing policy files, stale credentials, alternate API input paths, and normal application misconfiguration.

The owner/host administrator, container daemon, kernel/VM, trusted launcher, pinned Claude executable, and proxy remain trusted. This plan does not claim protection against host root, Docker-daemon compromise, container escapes, malicious vendor binaries, or credentials deliberately pasted into an approved source file. Pinning and updates reduce those risks; they do not eliminate them.

## 3. Code evidence the implementer must preserve in the issue ledger

All links below refer to the audited commit, not mutable master. Paths remain authoritative discovery anchors even if lines move.

| Actual behavior | Evidence | Required change |
| --- | --- | --- |
| Claude delivery containers mount the complete host Claude directory and sibling config file. | [spawn_launch.py](https://github.com/rennf93/roboco/blob/9d7747f65050f70489b26ec56b9df2d593bf4d87/roboco/runtime/engines/spawn_launch.py#L902-L970) | Dedicated credential store visible only to trusted Claude runtime; no project volumes there. |
| Delivery roles receive built-in Read/Write/Edit/Bash/Grep/Glob tools. | [spawn_launch.py](https://github.com/rennf93/roboco/blob/9d7747f65050f70489b26ec56b9df2d593bf4d87/roboco/runtime/engines/spawn_launch.py#L1220-L1249) | Empty built-in tool set; explicit remote MCP capability surface. |
| Routing may return the legacy Anthropic route after failures; strict mode covers only some branches. | [services/llm.py](https://github.com/rennf93/roboco/blob/9d7747f65050f70489b26ec56b9df2d593bf4d87/roboco/services/llm.py#L495-L565) | One mandatory subscription route, no legacy fallback. |
| Output extraction instantiates `AsyncAnthropic(api_key=settings.anthropic_api_key)`. | [extraction.py](https://github.com/rennf93/roboco/blob/9d7747f65050f70489b26ec56b9df2d593bf4d87/roboco/services/extraction.py#L490) | Deterministic extraction by default; optional model classification must use the same subscription broker. |
| Quality commands execute shell in the controller context. | [quality_gate.py](https://github.com/rennf93/roboco/blob/9d7747f65050f70489b26ec56b9df2d593bf4d87/roboco/services/gateway/quality_gate.py#L75-L95) | Execute exclusively in credential-free workers. |
| Workspace dependency commands inherit essentially the entire process environment. | [workspace.py](https://github.com/rennf93/roboco/blob/9d7747f65050f70489b26ec56b9df2d593bf4d87/roboco/services/workspace.py#L463-L489) | Environment allowlist and separate offline build worker. |
| Optional security telemetry targets the developer's Guard Core service. | [security.py](https://github.com/rennf93/roboco/blob/9d7747f65050f70489b26ec56b9df2d593bf4d87/roboco/security.py#L847-L875) | Remove sender configuration/dependency and reject attempts to enable it. |
| Registry Compose mounts Docker socket and personal provider directories; publishes backing services; “local” generation uses a cloud model. | [registry compose](https://github.com/rennf93/roboco/blob/9d7747f65050f70489b26ec56b9df2d593bf4d87/docker-compose.registry.yml) | A separate canonical personal deployment with no unsafe alternate launch path. |
| Project HTML can be returned from the authenticated app origin. | [video.py](https://github.com/rennf93/roboco/blob/9d7747f65050f70489b26ec56b9df2d593bf4d87/roboco/api/routes/video.py#L163-L204) | Disable active previews in v1; no project JS/HTML on dashboard origin. |
| MCP code can expose a broad default registry if a manifest is missing. | `roboco/mcp/do_server.py`, `flow_server.py` and callers | Missing/malformed/expired policy must expose zero tools and refuse the run. |

Current upstream `AGENTS.md` contains older paths and descriptions. For example, the frontend is actually `panel/`, not `roboco/panel/`; actual model/provider code and Compose must be inspected rather than trusting those descriptions.

Previously inspected Docker images were built on 2026-10-16, older than this source snapshot. Do not combine those images with the new source or reuse `latest` as release evidence. The prior binary comparisons establish provenance for those inspected artifacts, not safety or compatibility of a future build.

## 4. Chosen architecture and alternatives

### 4.1 Alternatives considered

1. **Environment flags plus read-only credential mounts:** rejected. Read-only tokens remain readable, shell filtering can be bypassed, and controller-side scripts inherit secrets.
2. **Put current RoboCo unchanged in a VM:** useful as an outer boundary but insufficient. Project code still shares the VM/container namespace with provider credentials.
3. **Separate trusted model runtime from all project execution, backed by externally enforced networking:** selected because it satisfies the owner's requirements. Docker Desktop's Linux VM is the initial platform; separate containers establish the application boundaries. A dedicated Linux VM can be added later as defense in depth, not as a replacement for those boundaries.

This is one security project delivered in dependency-ordered tasks, not independent feature work to merge in arbitrary order. No real projects or real credentials are admitted until the relevant end-to-end gates pass.

### 4.2 Components and data flow

```text
Owner browser -- loopback/authenticated --> panel + controller -- internal --> PostgreSQL/Redis
                                              |
                                              | typed RunSpec / run-scoped capability
                                              v
                                      trusted Claude service
                                      - official CLI only
                                      - dedicated login volume
                                      - no project filesystem
                                      - no built-in model tools
                                              |
                          inference/auth      |      explicit MCP tools
                          through proxy       +-----------> tool gateway
                              |                               |
                              v                               | typed worker job
                         Anthropic                            v
                                                    trusted fixed-profile launcher
                                                              |
                                                              v
                                                    disposable offline worker
                                                    - one sanitized workspace
                                                    - no credential mounts
                                                    - no control sockets/tokens
                                                    - no internet or data network
```

The launcher is the only application component with Docker-daemon access. Its API is **not** a generic Docker-socket proxy: it accepts only validated fixed job profiles and opaque, registered workspace handles. It cannot accept arbitrary images, host paths, bind mounts, networks, capabilities, devices, entrypoints, environment variables, or Docker flags from controller/model requests. A compromise of this launcher remains in the trusted-base threat boundary; keep it small and do not run repository code there.

Provider credentials are absent from controller, gateway, launcher request bodies, task workers, task DB rows, panel responses, logs, exported patches, and test artifacts. The launcher can cause the daemon to mount a predetermined named credential volume into the trusted Claude service, but it never reads or returns its contents. Do not expose Docker's socket in the Claude service either.

Only the Claude service may request provider-bound internet traffic. Workers use `--network=none`; they communicate through container stdin/stdout supervised by the launcher, not via an exposed service or a mounted control socket. Do not assume clearing `HTTP_PROXY` alone blocks a network.

### 4.3 Credential lifecycle

- Dedicated Docker named volume, e.g. `roboco_personal_claude_auth`, mounted only in the trusted Claude service and owner-only login/maintenance job. Files `0600`, containing directory `0700`, dedicated non-root runtime UID. Validate these permissions instead of assuming the volume is protected.
- Use supported official CLI login inside that dedicated Linux context. macOS Keychain does not automatically become available through a directory bind mount. Do not export the owner's personal Keychain entry or clone their full home directory.
- Prefer the ordinary supported subscription login flow. Do not implement custom OAuth refresh requests, parse raw refresh tokens into the controller, or build a fake Anthropic-compatible API using subscription tokens.
- Runtime auth checks call the official CLI's auth-status command under a fixed clean environment and parse a pinned, fixture-tested schema. Only return redacted status metadata. Unknown schema or uncertain billing/auth mode blocks dispatch.
- One CLI process owns refresh at a time. Explicit login/logout/reauth uses the same lock and cannot race a run. Refresh failure pauses work; revoke/log out only through an owner command after describing its scope.
- Do not use environment-based provider tokens in this first implementation. Reject `CLAUDE_CODE_OAUTH_TOKEN`, arbitrary `ANTHROPIC_AUTH_TOKEN`, API-key variables, profile/gateway overrides, helper scripts, and provider-selection variables in runtime input. The persisted official subscription login is the single supported method.
- Disable account-level paid extra/usage credits and top-ups before the first real run. CLI auth metadata does not certify that billing setting; retain a local owner setup acknowledgement without storing account screenshots or secrets.

### 4.4 CLI and MCP restrictions

Implement a single argv/environment builder used by every role and helper. Candidate controls to prove against the pinned CLI in Task 1 include `--restricted`, `--tools ""`, `--strict-mcp-config`, trusted `--mcp-config`, an empty setting-source list, trusted `--settings`, disabled slash commands, and no session persistence. The official CLI reference distinguishes built-in tool restriction from MCP restriction; allow only the explicitly provisioned MCP tools. Do not pass `--bare`: its authentication behavior is unsuitable for this subscription design. Do not use permission-bypass or auto-permission modes.

The Claude service's cwd is a trusted empty directory, not a project root. It cannot mount `.git`, project `CLAUDE.md`, `.claude`, `.mcp.json`, node_modules, shell startup files, user plugins, arbitrary hook commands, or user-provided binaries. Project instructions can be admitted as **untrusted text** through the file gateway; they cannot configure the trusted runtime. Disable automatic plugin installation, remote-control/cloud/session-sync features, and nonessential traffic. Accept no arbitrary CLI flag from a task or API request.

All file reads/edits/searches and shell commands use custom MCP tools backed by the worker. Existing flow/do verbs keep their explicit role gates; no model-generated path, URL, or tool name is sufficient authority. Background tool-free model requests use the same CLI builder with zero MCP tools.

CLI docs evolve. The listed controls are a candidate contract, not proof that an arbitrary CLI version enforces it. If the pinned official CLI cannot support subscription auth, a genuinely empty built-in tool set, exact MCP control, and the necessary network restrictions together, **stop at the feasibility gate**. Report the incompatible control; do not weaken the design or substitute API keys.

### 4.5 Filesystem and project admission

- Initial import is an owner-operated sanitized snapshot, not a live bind of a personal repository/home. Admit regular files and directories only in v1; reject symlinks, hardlinks, devices, FIFOs, socket files, absolute paths, traversal, and suspicious archive metadata. This intentionally excludes projects that require source symlinks until a separately designed safe policy exists.
- Exclude `.git`, auth/config directories, `.env` and secret variants, private-key files, `.npmrc`, `.pypirc`, cloud/SSH config, local database dumps and known credential files. Platform deny rules are mandatory; a project ignore file can only tighten them. Scan the resulting snapshot for suspected secrets and ask the owner to resolve matches; scanning is not the proof of isolation.
- Initialize task-local Git history inside the offline workspace. No original remotes or credential helpers enter it. Git hooks, signing commands, filters and project package scripts execute only inside offline workers. Never run `git`, `make`, package scripts, project Python imports, or test discovery from a privileged controller checkout.
- A workspace handle is bound to owner/project/run IDs in server-side state. Each worker mounts exactly one registered workspace. Credential stores, other workspaces, daemon sockets and controller data are physically absent, not merely hidden with a prompt.
- The file gateway resolves relative paths beneath the workspace using descriptor-relative no-follow operations; reject symlinks at every segment and reject nonregular final objects. A check-then-open `Path.resolve()` is insufficient against concurrent mutation.

### 4.6 Egress policy

A plain CONNECT hostname allowlist does not distinguish telemetry from inference when they share a host, nor does it enforce HTTP paths. Use a mature, pinned proxy capable of validating HTTP authority, method, path and upstream identity over TLS, with a small trusted policy module. Initial implementation candidate: a pinned `mitmproxy` service plus an addon in this fork. Its private CA and any auth material are trusted-runtime-only; verify CLI compatibility before committing to this mechanism.

First run the compatibility probe with synthetic authentication and a fake provider inside isolated test networking, without project mounts. Before real login is available, use only documented first-party candidate destinations in the separate owner-maintenance policy; do not enable unrestricted discovery. During the owner-approved feasibility test, observe the **official CLI's** required authentication and inference destinations with a synthetic prompt and dedicated account, then create an explicit versioned normal-run endpoint-policy file. Do not invent endpoints from memory, allow `*.anthropic.com`, enable generic CONNECT tunneling, or use a user-controlled endpoint discovered from task content. Some legitimate endpoint paths contain dynamic IDs; allow only validated segments for a demonstrated essential operation, with negative tests for sibling paths. Capture no real prompts, tokens or response bodies in the saved policy evidence.

Reject alternate authorities, IP literals, mismatched SNI/Host/CONNECT targets, redirects to unapproved destinations, WebSockets/HTTP upgrades unless proven essential, alternate ports, private/link-local/metadata destinations, IPv6 bypass, and DNS rebinding. Pin resolution per connection; do not validate one address and dial another. All direct external packets from the Claude runtime must be denied independently of its proxy environment. Only the proxy has the controlled external interface. Enforce this in the supported Docker Desktop/Linux environment, and test Docker's embedded DNS, host-gateway access and proxy-death behavior explicitly.

For normal runs, permit only essential inference/auth operations and internal run-scoped MCP connectivity. Disable telemetry/feedback/share/update/plugin endpoints even if they are on an approved domain. The owner-only maintenance login mode is a different profile, cannot run tasks, and cannot retain broader permissions after login completes. If proxy/TLS compatibility prevents this level of control, the strict network claim is **not achieved**; stop rather than silently shipping hostname-only protection.

## 5. Stable interfaces for the implementation

Create `roboco/runtime/subscription/` and keep responsibilities separate. Each run has a maximum duration of 1,800 seconds and 32 agentic turns; the queue holds at most 64 pending runs. Exceeding a limit produces an explicit incomplete/blocked result. Increasing these trusted limits requires an owner configuration change, never a task argument. Names below are part of this handoff; changing them requires updating all dependent tasks and tests in the same change.

### 5.1 Data types in `contracts.py`

Use frozen Pydantic models with `extra="forbid"`, UUID identifiers, bounded UTF-8 inputs, and no free-form env/argv/URL/mount fields.

- `SubscriptionState`: `ready | login_required | quota_exhausted | policy_blocked | configuration_error`.
- `SubscriptionStatus(state, authenticated: bool, auth_method: str | None, checked_at: datetime, reason_code: str | None)`. No email, raw auth JSON, token, credential path, or key payload.
- `RunSpec(run_id, project_id: UUID | None, task_id: UUID | None, agent_id: UUID | None, purpose, model, prompt: str, workspace_handle: UUID | None)`. `purpose` is `delivery | intake | secretary | background`; model must match a trusted operator-maintained allowlist of exact full Claude model IDs, pinned with the CLI contract; aliases that can silently select a different model are not accepted. `prompt` <= 1 MiB; no secret file-path attachment expansion. A supplied project/run association is checked in server state.
- `RunResult(run_id, state, text: str, reason_code: str | None, usage: dict[str, int])`. `state` is `completed | login_required | quota_exhausted | blocked | failed | cancelled`. Usage is counters, not an assertion of API billing cost. Terminal partial text is marked incomplete.
- `WorkerOperation`: `read_file | list_files | search_text | write_file | apply_patch | run_command | export_patch`.
- `WorkerJob(job_id, run_id, project_id, workspace_handle, operation, payload)`. Discriminated payload models per operation; relative paths only. Shell command text is permitted only in `run_command` payload and is executed only by the offline worker. Arbitrary command capability never changes network/mount/security profile.
- `WorkerResult(job_id, exit_code: int | None, stdout: str, stderr: str, truncated: bool, reason_code: str | None)`. Output limit: 64 KiB per stream; file read/write limit: 1 MiB per operation. Return a structured limit error, not a silent truncated edit. Output truncation must be explicit.
- Fixed trusted defaults: command timeout 600 seconds, 2 CPU, 2 GiB memory, PID limit 256; non-root, read-only image root, required writable workspace/tmp only, drop all capabilities, no-new-privileges, default seccomp, network none. User/task payloads cannot raise these limits.

### 5.2 Service contracts

- `policy.py`: `validate_subscription_policy(settings: Settings) -> None`; `build_claude_env() -> dict[str, str]`. Environment is constructed from an allowlist; no `dict(os.environ)` and remove-list approach.
- `auth.py`: `async inspect_subscription() -> SubscriptionStatus`. Official auth-status subprocess only, under the same protected launcher/credential lock as model runs. Do not expose raw stdout/stderr to controller logs.
- `cli.py`: `build_claude_argv(spec: RunSpec, *, mcp_config: Path | None, settings_file: Path) -> list[str]`. All paths supplied by trusted setup, prompt via stdin, fixed cwd. No shell invocation, no arbitrary task argv.
- `service.py`: `async run(spec: RunSpec) -> RunResult`; `async cancel(run_id: UUID) -> None`. Per-profile concurrency lock, bounded queue, parse official streaming output, zero raw CLI event persistence.
- `client.py`: async `SubscriptionClient.status() -> SubscriptionStatus`, `SubscriptionClient.run(spec: RunSpec) -> RunResult`, and `SubscriptionClient.cancel(run_id: UUID) -> None`. Control-plane calls use an internal authenticated transport with exactly status, validated run submission and cancellation operations; it carries run authority, not provider auth. The transport is configuration-fixed, never task-supplied. Enforce identical Pydantic validation on both ends. Do not expose a generic public model-completion endpoint.
- `launcher_client.py`: `WorkerLauncher.execute(job: WorkerJob) -> WorkerResult`. Small internal job API to the fixed-profile launcher; run registration, workspace ownership and replay checks occur server-side, not just in the client.
- `worker_files.py`: safe file operations beneath one workspace. `worker_entrypoint.py`: validate a job and execute it with a clean environment. Both run only in credential-free containers.
- `tool_gateway.py`: bind permitted role tools to one active run; convert tool requests to `WorkerJob`. Absent/invalid/expired manifests and cross-run capabilities always deny.

A run-scoped MCP/control capability is still a sensitive authorization token, even though it is not a Claude credential. Keep it in the trusted CLI/MCP transport, never in a worker environment or workspace. Bind it to run/project/role, expire it at run end, and revoke on cancellation. Do not rely solely on a bearer string supplied by model arguments.

## 6. Source map and file ownership

These are modification anchors, not instructions to rewrite unrelated modules.

| Responsibility | Existing files to modify | New files/directories |
| --- | --- | --- |
| Hard policy and configuration | `roboco/config.py`, `roboco/services/settings.py`, `roboco/api/routes/settings.py`, schemas | `roboco/runtime/subscription/contracts.py`, `policy.py` |
| Subscription runtime | `roboco/llm/providers/claude_code.py`, `roboco/llm/providers/registry.py`, `roboco/services/llm.py` | `auth.py`, `cli.py`, `service.py`, `client.py` in the new package |
| Fixed-profile launcher/workers | `roboco/runtime/engines/spawn_launch.py`, `spawn_config.py`, sandbox lifecycle callers | `launcher_client.py`, `worker_entrypoint.py`, `worker_files.py`, `scripts/personal/launcher.py` |
| Delivery/chat integration | `roboco/agent_sdk/intake_driver.py`, `secretary_driver.py`, their main entrypoints, `live_providers.py`, spawn/reaper callers | Shared subscription session adapter, only if needed to fit existing interfaces |
| Project tools/quality/dependencies | `roboco/mcp/do_server.py`, `flow_server.py`, `utils.py`, `roboco/services/gateway/quality_gate.py`, `content_actions.py`, `roboco/services/workspace.py` | `tool_gateway.py`, snapshot/import/export implementation under `roboco/services/` |
| Background generation and retrieval | `extraction.py`, `memory_distiller.py`, `optimal.py`, `optimal_brain/`, `vault_intake_engine.py`, `x_engine.py`, `video_engine.py`, `roboco/eval/runner.py` | One shared subscription-backed helper; no separate raw HTTP model clients |
| Local review lifecycle | `roboco/foundation/policy/lifecycle.py`, `roboco/services/gateway/choreographer/`, forge/workspace callers and existing UI | Explicit personal local-review adapter and local artifact schemas |
| App authentication/browser boundary | `roboco/api/auth/`, `roboco/api/deps.py`, `roboco/api/routes/video.py`, `deploy/nginx.conf` | Personal nginx configuration if separating canonical profile requires it |
| Provider/settings UI | `panel/src/components/settings/ai-routing-card.tsx`, `provider-key-card.tsx`, `feature-flags-card.tsx`, `panel/src/lib/api/providers.ts`, related schemas/hooks/routes | Subscription status/login instruction component and tests |
| Deploy/build/network | `docker/agent-base.Dockerfile`, `docker/orchestrator.Dockerfile`, `docker/panel.Dockerfile`, `Makefile`, bootstrap/workflow callers | `compose.personal.yml`, `docker/claude-subscription.Dockerfile`, `docker/task-worker.Dockerfile`, `deploy/personal/egress-policy.json`, proxy addon/config, trusted runtime settings |
| Verification and handoff | Existing unit/integration conventions and frontend tests | `tests/unit/subscription/`, `tests/integration/subscription/`, `tests/security/subscription/`, `scripts/personal/verify_*`, `docs/personal/` |

Use this source search to find every remaining direct model client, including dynamic URL construction, rather than assuming this list is exhaustive:

```bash
rg -n 'AsyncAnthropic|Anthropic\(|chat/completions|/api/chat|/api/generate|local_llm_model|ollama|anthropic_api_key|provider_base_url|provider_auth_token' roboco docker scripts
rg -n 'create_subprocess|subprocess\.|shell=True|os\.system|exec\(' roboco
rg -n 'credentials|ANTHROPIC_|CLAUDE_CODE_OAUTH|\.claude|docker.sock|host.docker.internal|network_mode' roboco docker deploy scripts compose* docker-compose*
```

Investigate actual reachable call paths. Documentation/test fixtures that mention excluded providers may remain explanatory; active alternate dispatch/build/bootstrap paths may not remain enabled.

## 7. Global constraints

1. `Claude subscription only; API keys and provider fallback are forbidden.` Enforce at startup, persisted settings writes, provider API inputs, runtime dispatch, and network boundary.
2. `Provider credentials never enter a project worker, controller response, tool payload, task log, or project filesystem.` Official Claude runtime access is the explicit exception needed to authenticate.
3. `Project code never executes in the controller, launcher, proxy, or authenticated Claude service.`
4. `All project operations use one sanitized, registered workspace and run-scoped authorization.`
5. `Normal task workers have no network and no control sockets.`
6. `Every model call, including background work and interactive chats, uses the same subscription service.` Required unsupported features must stop explicitly, not use a secondary model.
7. `Unknown auth modes, malformed policy, missing manifests, expired run authority, and network-policy failure block work.`
8. `No model/task/UI setting can weaken authentication, egress, tool isolation, or billing policy.`
9. `No personal host auth directories, Keychain exports, SSH agent, Git credential helper, cloud credentials, or arbitrary host path is mounted.`
10. `Normal execution is credential-free offline project work plus reviewed Anthropic inference/auth traffic.` Maintenance has a separate profile with no task/project mounts.
11. `Do not use real secrets or private projects to develop the security tests.` Synthetic fixtures only until gates pass.
12. `No deployment, merge, account logout, secret deletion, paid-usage enablement, or destructive cleanup is implicitly authorized by this document.`

## 8. Review focus

Pin these five input classes in the tests named below; do not treat an unchanged file as exempt from the security contract.

| Condition | Required behavior | Owning task/test |
| --- | --- | --- |
| A valid subscription session coexists with an inherited API key, helper, Console login, custom endpoint or account paid-usage setting. | Reject unsafe auth configuration; no billing-method fallback; expose the account-setting limitation clearly. | Tasks 1–2, `test_mixed_auth_is_rejected`, `test_unknown_auth_status_blocks`, account setup gate |
| A hostile build script edits a symlink between validation and open, reads `/proc`, or fabricates a sibling workspace/run ID. | No provider/control credentials or cross-project files reachable. | Tasks 4–5, `test_file_race_cannot_escape`, `test_worker_cannot_read_parent_env`, `test_cross_run_job_is_denied` |
| CLI upgrade or a repo instruction enables new built-in tools, hooks, plugins, a child agent or unexpected config source. | Trusted runtime still exposes only exact provisioned MCP capabilities, or version check stops startup. | Tasks 1, 6, 12, `test_unapproved_cli_contract_blocks`, `test_hostile_project_config_is_inert` |
| Inference/auth shares a host with telemetry, redirects, private targets or alternate TLS authorities. | Only essential reviewed operations pass; no direct, DNS, IPv6 or proxy-down bypass. | Task 10, `test_same_host_telemetry_is_denied`, live namespace capture |
| A background retry, interactive resume, failed quality gate, stream disconnect or restart occurs. | Same security boundary; no legacy route, duplicated write, lost terminal state, leaked partial transcript or false success. | Tasks 7–9, 11–13, interruption/replay/failure matrix |

## 9. Task 0 — Establish the personal fork and reproducible baseline

**Files:** fork metadata/remotes; `docs/personal/implementation-status.md`; repository-local copy of this plan at `docs/personal/claude-subscription-hardening-plan.md`.

**Consumes:** §§1–8. **Produces:** an isolated personal checkout, recorded upstream SHA, task ledger and baseline commands. No credentials or running services.

- [x] Personal fork and checkout prepared: `PramodTKodag/roboco`, `/Users/thesilentknight/Projects/Personal/roboco`, branch `docs/security/claude-subscription-hardening`; `origin` points to the personal fork and `upstream` to `rennf93/roboco`. The implementation session must re-verify this state and preserve unrelated work; no fork recreation or company-checkout modification.
- [ ] Read actual Makefile bodies and test fixtures before running checks. Record Python/package-manager versions from the repository, current hooks, source SHA and dirty state. Do not discard existing changes.
- [ ] Copy this document into the fork; create a short status ledger with task IDs, commit/check evidence and unresolved blockers. Record that the security architecture is the target, not current behavior.
- [ ] Run a read-only baseline inventory of model clients, subprocesses, auth mounts, provider APIs, alternate Compose/bootstrap paths and frontend external resource loads. Store path/symbol findings, not user configuration values.
- [ ] Establish isolated test DB/Redis names and fixture directories. Never point tests at the owner's normal stack. Run relevant existing routing/quality/auth tests and record failures without waiving security-relevant ones.
- [ ] Commit the plan/baseline ledger on a work branch using repository conventions; no publication or implementation claim yet.

## 10. Implementation tasks

### Task 1 — Prove official CLI subscription and tool/network isolation before integrating it

**Files:** create `tests/integration/subscription/test_cli_contract.py`, `tests/security/subscription/fixtures/`, `scripts/personal/probe_cli_contract.py`, `docs/personal/cli-contract.md`; initial trusted-only Dockerfile/proxy test fixtures.

**Consumes:** target CLI restrictions in §4 and source baseline. **Produces:** an exact CLI version/artifact checksum, redacted auth/stream fixtures, verified flags/schema, and essential endpoint inventory for Task 10.

- [ ] Write failing contract tests: an inherited synthetic API key/helper/custom endpoint is refused; API/Console auth is refused; unknown JSON auth status is refused; an attempted built-in Read/Bash/Task/WebFetch call never executes; only the explicit test MCP tool is available.
- [ ] Run `uv run pytest tests/integration/subscription/test_cli_contract.py -q --no-cov` in its synthetic/offline mode. Expected: failures establishing the missing wrapper; no real login or external request.
- [ ] Select a supported, patched official Claude CLI version and pin its package/binary integrity. Initial previously audited image candidate was `2.1.301`, Linux x64 SHA256 `bc1aad9982c7b59c7d889ff92595d61cf46e0a6eae074cb551d932a989bcd222`; this is provenance evidence only, not a demand to ship that older version. Revalidate current vendor release/security information. Do not use floating versions or execute an unverified installer.
- [ ] Build a disposable probe containing only the official CLI, trusted settings, fake MCP endpoint and empty cwd. Verify argv behavior and the actual auth-status/stream schema. Parse both application errors and CLI exit status; success exit alone is insufficient.
- [ ] At the owner-operated setup checkpoint, have the owner log into a **new dedicated** Linux runtime profile using the official subscription flow. No personal auth exports, Keychain reads, token paste into this plan/chat or API key. Verify a benign tool-free prompt uses subscription auth. This is the only owner-dependent prerequisite; continue all synthetic tasks while waiting.
- [ ] Test TLS proxy compatibility and exact MCP restriction with this same pinned build. Save only domain/method/path patterns, version, checksum and redacted outcomes. A fake auth fixture cannot count as live subscription validation.
- [ ] If any required control cannot be enforced, stop release work and report the precise incompatibility. Do not make API keys, broad egress or shared credential mounts an alternative implementation.
- [ ] Commit contract/probe fixtures and the redacted evidence. Live test remains separately opt-in and skipped with an explicit reason when account setup has not happened.

### Task 2 — Add immutable subscription-only policy and safe status

**Files:** new `roboco/runtime/subscription/{__init__,contracts,policy,auth}.py`; modify `roboco/config.py`, `roboco/services/settings.py`, settings schemas/routes; tests `tests/unit/subscription/test_policy.py`, `test_auth.py`.

**Consumes:** Task 1's pinned CLI/auth schema. **Produces:** `SubscriptionStatus`, `validate_subscription_policy`, clean environment builder and official-client status inspection.

- [ ] Write failing tests `test_mixed_auth_is_rejected`, `test_unknown_auth_status_blocks`, `test_console_auth_is_rejected`, `test_policy_cannot_be_overridden_by_db`, `test_status_is_redacted`, `test_cli_env_is_constructed_from_allowlist`. Assert unsafe env/config values never appear in process argv, logs or returned status.
- [ ] Run `uv run pytest tests/unit/subscription/test_policy.py tests/unit/subscription/test_auth.py -q --no-cov`; establish real missing-behavior failures.
- [ ] Implement §5 contracts and validation. Keep provider secret/URL fields out of new public schemas; reject legacy API-key and unsupported-provider writes with a structured policy error. If an inherited auth override is present, fail with its **variable name only**, not its value. Reject conflicting Docker/runtime settings too.
- [ ] Implement `inspect_subscription()` through the official CLI, bounded timeout and redacted parser. Do not read raw credential files in app/controller code. Treat invalid status schema, missing session or uncertain provider as non-ready.
- [ ] Add owner setup checks/instructions for disabling paid extra/usage credits and automatic top-ups. Clearly distinguish the local acknowledgement from provider-enforced billing controls. Never promise that `authMethod=claude.ai` alone proves no paid overage.
- [ ] Run the named tests and contract tests. Expected: every invalid auth/policy case blocks before dispatch, without a provider request. Commit this independently reviewable policy unit.

### Task 3 — Implement the fixed-profile launcher and server-side workspace ownership

**Files:** new `scripts/personal/launcher.py`, `roboco/runtime/subscription/launcher_client.py`, fixed profile definitions under `deploy/personal/`; new `tests/unit/subscription/test_launcher_policy.py`, `tests/integration/subscription/test_launcher.py`; modify only the container-lifecycle adapters that need this contract.

**Consumes:** `WorkerJob`/`WorkerResult`. **Produces:** `WorkerLauncher.execute`, run registration and cancellation; the only Docker-capable application component.

- [ ] Write failing tests for arbitrary image/argv/env/mount/network/device/capability fields, unknown operations, oversized jobs, unknown workspace handles, cross-project/run requests, expired/replayed jobs and caller-supplied host paths. They must fail server-side even if a client skips validation.
- [ ] Run `uv run pytest tests/unit/subscription/test_launcher_policy.py -q --no-cov`; expected missing-policy failures.
- [ ] Implement allowlisted immutable images and security profiles, server-side workspace registration and per-run capability validation. Requests identify opaque workspace handles only. Mount sources derive from a trusted registry, never string concatenation with task-supplied paths.
- [ ] Spawn each operation as a disposable non-root worker with §5 resource limits, network none, no control sockets, no secrets and one workspace volume. Stream jobs/results over daemon-supervised stdin/stdout; project processes receive no launcher credential or daemon socket. Bound queues and output memory.
- [ ] Cancellation/timeout kills and reaps the full container/process group; restart reconciliation identifies owned jobs by immutable labels and run IDs without touching unrelated containers/volumes. Cleanup must have a safe inspect/dry-run path and ownership checks.
- [ ] Run the integration test in a disposable test project. Assert actual Docker inspection reports the expected mounts, networks, user, caps and restrictions. A mocked `docker run` argument assertion is not enough.
- [ ] Commit launcher plus evidence. Do not replace it with a generic Docker-socket proxy or restore controller access for compatibility.

### Task 4 — Import sanitized workspaces and provide safe file operations

**Files:** new `roboco/services/personal_workspace.py`, `roboco/runtime/subscription/{worker_files,worker_entrypoint}.py`, worker Dockerfile; tests `tests/unit/subscription/test_workspace_admission.py`, `test_worker_files.py`, `tests/security/subscription/test_filesystem_isolation.py`.

**Consumes:** launcher registry and workspace handles. **Produces:** owner-operated import/export and worker file operations under §4.5.

- [ ] Write failing tests for archive absolute/traversal paths, encoded traversal, duplicate entries, symlink/hardlink/device/FIFO/socket entries, oversized files, secret/config exclusions, attempts to weaken excludes, and project A naming project B's handle.
- [ ] Write race tests that swap parent directories and final files while reads/writes execute. Assert no read/write outside the one workspace; reject nonregular objects. Test direct `/proc`, host paths and sibling paths from an actual worker.
- [ ] Run `uv run pytest tests/unit/subscription/test_workspace_admission.py tests/unit/subscription/test_worker_files.py -q --no-cov`; confirm meaningful failures.
- [ ] Implement snapshot validation and descriptor-relative no-follow file access. Import into a dedicated workspace volume and create local Git state **inside the worker** with no original `.git` or credential helpers. Do not execute project code during import or in the controller.
- [ ] Implement bounded file/list/search/patch tools with explicit encoding, size and path errors. A patch must not silently modify denied paths or traverse links. Export only reviewed workspace changes, never runtime configuration, credentials or controller state.
- [ ] Run actual-namespace isolation/race tests with synthetic credential stores mounted exclusively in the trusted test service. Assert worker attempts return ENOENT/EACCES, cross-project reads fail, and provider sentinel bytes are absent from all results.
- [ ] Commit workspace/file isolation. Document the intentional v1 symlink exclusion and offline-only import workflow.

### Task 5 — Move every project command and quality gate out of the controller

**Files:** `roboco/services/gateway/quality_gate.py`, `roboco/services/workspace.py`, reachable subprocess legs in `content_actions.py`, workspace/forge helpers, new worker command handler; existing `tests/unit/gateway/test_quality_gate.py`; new `tests/security/subscription/test_project_execution.py`.

**Consumes:** worker file/job API. **Produces:** one offline execution path for shell, Git, build, package scripts, lint, tests and project-generated commands.

- [ ] Write failing tests where a project quality command/build backend/Makefile/Git hook prints environment, reads credential paths, inspects process environments, accesses the host gateway, starts child processes and attempts a network upload. Use synthetic secret values outside the worker; assert they never appear in results.
- [ ] Add `test_quality_infra_error_blocks_submission`: unavailable worker, cancelled job or malformed result cannot be reported as passed/skipped. Explicitly no configured quality command may remain a legitimate operator-selected skip; infrastructure failure is not such a skip.
- [ ] Run the targeted existing quality/workspace tests plus new synthetic tests; record the current controller execution failures.
- [ ] Replace controller-side project shell/dependency/Git operations with `WorkerJob`s. Build env from a positive allowlist; no inherited provider, database, cloud, Git, proxy or run-capability variables. Keep trusted app subprocesses such as official auth-status separate and documented.
- [ ] Default project dependency installation off. Implement locked, pre-fetched offline dependency consumption only; forbid controller `uv sync`, `pip`, `pnpm install`, project imports/test discovery and unrestricted network acquisition for a task. Return actionable missing-package errors.
- [ ] Re-run the actual worker attack tests, timeout/child-process cleanup tests, and relevant quality lifecycle tests. Static scan for remaining subprocess sites; every project-controlled site must map to an offline job, including less common content-action paths.
- [ ] Commit the complete project-execution boundary. Do not retain an opt-out compatibility flag for privileged execution.

### Task 6 — Implement the credential-bearing Claude service and capability-only MCP tools

**Files:** `roboco/runtime/subscription/{cli,service,client,tool_gateway}.py`, trusted runtime settings, `docker/claude-subscription.Dockerfile`; `roboco/mcp/do_server.py`, `flow_server.py`, `utils.py`; tests `tests/unit/subscription/test_cli.py`, `test_tool_gateway.py`, `tests/security/subscription/test_runtime_boundary.py`.

**Consumes:** auth/policy, exact CLI contract, worker APIs and registered workspaces. **Produces:** `run`, `cancel`, bounded streaming and run-scoped MCP surface.

- [ ] Write failing tests for arbitrary CLI settings, built-in tool names, project hooks/plugins/MCP configuration, file-mention expansion, child agents, remote tools and missing/invalid manifests. A missing manifest exposes **zero** tools and refuses the run.
- [ ] Add `test_cross_run_job_is_denied`, stale capability replay, role escalation and model-supplied run/project IDs. Authorization must use the authenticated transport's registered run rather than an argument supplied by the model.
- [ ] Run `uv run pytest tests/unit/subscription/test_cli.py tests/unit/subscription/test_tool_gateway.py -q --no-cov` and establish failures.
- [ ] Implement the single CLI builder, trusted service and typed `SubscriptionClient` transport. Supply prompt over stdin, fixed empty cwd, exact tools/settings/MCP config, clean env and protected auth mount. Keep model responses/data distinct from command/control messages. Tool-free background mode must not accidentally inherit delivery tools.
- [ ] Translate model file/command requests into gateway operations executed solely by workers. Retain existing role-scoped business verbs, but audit them for controller-side project execution, filesystem reads, URLs, secrets and notification side effects. Use exact tool-name comparisons, not substring matches for trusted server names.
- [ ] Apply the single-process credential lock to auth inspection, login maintenance and runs. Parse stream terminal/error events, bound partial output, make cancellation revoke capabilities, and prevent authenticated CLI state from entering tool results or raw task logs.
- [ ] Run the real CLI/MCP contract probe and actual mount/process isolation tests. A hostile project cannot configure the CLI because it is not mounted; direct Read/Bash cannot run because no such tool is exposed. Check this with attempted calls, not only argv snapshots.
- [ ] Commit the protected runtime. Keep real subscription smoke opt-in and report whether it ran.

### Task 7 — Route all delivery, Intake and Secretary sessions through the service

**Files:** `roboco/services/llm.py`, `roboco/llm/providers/{claude_code,registry}.py`, `roboco/runtime/engines/{spawn_launch,spawn_config,spawn_exit}.py`, `roboco/agent_sdk/{intake_driver,intake_main,secretary_driver,secretary_main,live_main,live_providers}.py`, relevant reaper/session callers; new tests `tests/unit/subscription/test_dispatch.py`, `test_interactive_sessions.py`.

**Consumes:** `SubscriptionClient` and `RunSpec`. **Produces:** a single reachable provider and secure lifecycle for every model role.

- [ ] Write failing table-driven tests covering global/role/agent/complexity assignments, disabled/missing/invalid provider rows, decrypt failure, missing base URL, offline local endpoint, unsupported model, failed auth, unknown provider enum and stale routing presets. Every unsupported route must block; none may reach a legacy route.
- [ ] Add Intake/Secretary start, chat turn, stream, reconnect, cancellation and controller restart tests. Those paths must use the same protected runtime and exact tool policy; no remaining in-controller SDK subprocess with personal home access.
- [ ] Run the targeted tests plus existing `tests/unit/llm/test_routing_downgrade.py`, `tests/unit/runtime/test_provider_routing.py`, `test_spawn_strict_mcp.py`, and `tests/integration/test_llm_routing.py` in the isolated test setup. Rewrite tests that intentionally expected insecure fallback to assert the new contract; do not delete unrelated lifecycle coverage.
- [ ] Implement subscription-only route resolution and registry. Delete active provider fallback, custom OAuth refresh loops and auth mounts from supported paths. Unsupported provider endpoints must fail before decryption, catalog fetches or network access. Preserve historical data only as inert history; do not dispatch it.
- [ ] Replace direct agent-container launches and interactive SDK paths with service calls, preserving run/task attribution, cancellation and terminal state. The role hierarchy remains; those logical agents do not receive provider credentials or container control.
- [ ] Verify all roles with synthetic backend events. Test quota/auth errors, partial/disconnected streams, wrong run IDs and restart reconciliation. Completed text plus a later terminal error is failed/incomplete, never success.
- [ ] Commit delivery/chat integration. Record that the strict profile is not compatible with upstream multi-provider settings.

### Task 8 — Eliminate background API calls, cloud models and optional outbound integrations

**Files:** `roboco/services/{extraction,memory_distiller,optimal,vault_intake_engine,x_engine,video_engine}.py`, `roboco/services/optimal_brain/`, `roboco/eval/runner.py`, related startup/background job wiring, `roboco/security.py`, configuration and feature gates; tests `tests/unit/subscription/test_background_policy.py`, `tests/security/subscription/test_no_direct_model_clients.py`.

**Consumes:** tool-free `SubscriptionClient.run(... purpose="background")`. **Produces:** no reachable raw model API clients and explicit behavior for unsupported subsystems.

- [ ] Write failing tests that exercise each direct-model caller discovered by the §6 scan. Patch raw HTTP/direct SDK constructors to fail the test if invoked. Cover startup jobs, periodic retry, RAG answer synthesis, memory distillation, vault intake, output extraction, review/mentor/validator helpers and eval code that is reachable in supported operation.
- [ ] Run `uv run pytest tests/unit/subscription/test_background_policy.py tests/security/subscription/test_no_direct_model_clients.py -q --no-cov`; capture the direct API/cloud-call failures.
- [ ] Keep deterministic output parsing as the explicit default. If a user-enabled feature needs text generation/classification, call the same subscription service with no tools and schema-validated output. Missing auth/quota is an explicit error, not a reason to call another provider or silently succeed with invented output.
- [ ] Remove Ollama/cloud generation and model-pull services from the personal deployment. Disable semantic/vector RAG/indexing in v1 and cancel dependent startup watchers cleanly; expose `feature_unavailable` rather than successful empty search. Do not add an embedding provider or silently reintroduce Ollama to preserve optional features. Ordinary local document/file search through worker tools remains available.
- [ ] Remove Guard Core sender setup and optional sender packages. Lock research/search, social publishing, Telegram, external storage, remote sandboxes, remote previews, external PR watching and automatic external integrations off in both API and persisted settings. Disable related schedulers; blocking UI switches alone is insufficient.
- [ ] Remove API-key settings/forms and direct API SDK dependencies once no supported caller needs them; retain the official CLI runtime dependencies actually required. Update lockfiles by the repo's normal method, without executing project-controlled install hooks in a privileged context.
- [ ] Verify a clean boot performs zero catalog/model pulls, embeddings or third-party calls, and every forbidden feature returns a clear policy error. Commit all background/feature policy changes with a list of intentionally unavailable optional features.

### Task 9 — Support personal local review without forge credentials or fake PRs

**Files:** lifecycle policy, gateway choreographer and forge-triggering callers, project schemas/settings, workspace export code, task/review UI; tests `tests/integration/subscription/test_local_review_workflow.py`.

**Consumes:** sanitized local workspace/export APIs. **Produces:** an honest offline task-to-review flow compatible with strict egress.

- [ ] Write a failing end-to-end test: import synthetic project → task implementation → offline quality checks → local diff artifact → QA/reviewer → owner review/export. Assert no Git provider requests, no Git credential lookup, and no fake PR URL or external status.
- [ ] Add failure tests: quality worker unavailable, refused path, export blocked by suspected secret, QA failure, interrupted export, duplicate submission and owner rejection. No failed/unknown validation may pass the task.
- [ ] Run `uv run pytest tests/integration/subscription/test_local_review_workflow.py -q --no-cov` with disposable state; verify the existing PR prerequisite is the expected failure.
- [ ] Implement an explicit `local_review` workflow value, local artifact identifiers and corresponding guard behavior. Preserve real QA/owner gates; no compatibility shim that treats a missing PR as an approved PR. Historical upstream forge data may remain read-only, but cannot activate outbound actions.
- [ ] Provide owner-only reviewed patch/bundle export. The application does not push, publish, merge, deploy or export credentials. Include only allowed project changes and check the exported bytes independently.
- [ ] Run lifecycle and policy tests with review rejection/re-entry and replay. Commit personal local-review support as a separate reviewable task.

### Task 10 — Enforce normal-run egress independently of app settings

**Files:** `compose.personal.yml`, `deploy/personal/{egress-policy.json,proxy_addon.py,proxy.yaml}`, startup preflight, trusted CA provisioning, `tests/security/subscription/test_egress.py`, `scripts/personal/verify_network.py`.

**Consumes:** Task 1's actual versioned endpoint inventory, not guessed URLs. **Produces:** fail-closed normal and owner-maintenance network profiles plus packet-level evidence.

- [ ] Write failing policy tests for same-host telemetry/feedback/update endpoints, unapproved host/method/path, IP literal, misleading hostname suffix, authority mismatch, redirect, CONNECT tunneling, metadata/private IPs, DNS rebinding, IPv6 and unexpected content protocol. Test invalid/empty policy files block startup.
- [ ] Run `uv run pytest tests/security/subscription/test_egress.py -q --no-cov` in synthetic mode; confirm the current unrestricted network behavior does not satisfy the assertions.
- [ ] Implement a pinned TLS-aware proxy using §4.6, with CA material outside all project/controller mounts, no request/response/header bodies in logs, strict upstream certificate verification and reviewed policy version/hash. Do not give model-facing configuration control over its policy or trust roots.
- [ ] Apply actual network namespaces/firewall rules so runtime cannot bypass proxy using raw IPs, alternate DNS, IPv6 or host gateway. Controller/data services have no general outbound route. Worker network remains none. The launcher cannot change profiles on a task's request.
- [ ] Run probes **from the exact runtime and worker namespaces** with synthetic data: approved essential request succeeds; all canary collectors receive zero requests/bytes; direct egress and external DNS fail; stopping/misconfiguring proxy blocks all normal provider traffic; maintenance mode cannot coexist with a task.
- [ ] Perform one owner-approved benign subscription inference through the final policy. Capture only redacted destination/method/path class and packet counts. Never save TLS-decrypted credential-bearing traffic or full inference payloads to the evidence bundle.
- [ ] Commit policy and verification tooling. If Docker Desktop or the pinned CLI cannot enforce the stated boundary, report a release blocker and the exact missing mechanism; never mark hostname-only or env-only restrictions equivalent.

### Task 11 — Harden local application access, UI, previews and logs

**Files:** `deploy/nginx.conf` or personal variant, `roboco/api/{deps,auth/}`, settings/provider routes and schemas, `roboco/api/routes/video.py`, frontend settings/provider components and `panel/src/components/ui/markdown.tsx`, log/transcript persistence paths; tests under existing API/frontend conventions plus `tests/security/subscription/test_log_redaction.py`.

**Consumes:** subscription status and local review APIs. **Produces:** authenticated local dashboard with no credential forms, unsafe feature switches, active content or automatic external browser resources.

- [ ] Write failing tests: unauthenticated/forged agent headers cannot assume owner identity; CSRF/cross-origin requests fail; wrong Host headers fail; WebSocket authentication is required; provider secrets never appear in status/settings/errors; remote Markdown images and active project HTML do not load.
- [ ] Write log tests using synthetic OAuth/API/cookie/database/capability sentinels in errors, stdout, tool results and CLI failures. Confirm no secret-bearing argv/env/raw stream reaches saved logs or exception responses. Do not log first and redact only UI rendering.
- [ ] Run existing API auth tests and targeted frontend tests. Use actual `panel/package.json` commands; do not assume the stale `roboco/panel/` path.
- [ ] Publish only `127.0.0.1:3000`; keep DB, Redis, controller, subscription service, launcher and proxy ports internal. Remove nginx standing owner-token injection. Require real local-owner session authentication with CSRF protection, strict origin/Host validation, secure session handling and same-origin WebSocket checks. Do not call loopback binding an authentication substitute.
- [ ] Replace provider/API-key controls with redacted subscription status and owner-terminal login instructions. Hard policy is immutable from the panel. Disable active preview endpoints in v1 and restrict browser resources/connects to the local app; do not render project-supplied JS/HTML on the authenticated origin or automatically fetch external Markdown images.
- [ ] Stop raw Docker/CLI stream dumps to `/data/logs/agents` and uncontrolled provider transcript persistence. Persist sanitized task outputs and bounded operational metadata only. Document local retention; encrypt local storage with the owner's OS/full-disk facilities rather than claiming redaction means no sensitive code is stored.
- [ ] Run real browser/network checks plus unit/integration tests. An owner click opening an external site is distinct from an automatic application request; do not add such clicks to task workflows.
- [ ] Commit local UI/auth/log hardening and accurate retention limitations.

### Task 12 — Build a minimal, pinned personal deployment and safe maintenance flow

**Files:** personal Compose and Dockerfiles, Makefile personal targets, `scripts/bootstrap.sh` and alternate quickstart callers, `.dockerignore`, CI workflows/registry references, `docs/personal/setup.md`; tests `tests/unit/subscription/test_deployment_policy.py` and startup CLI contract tests.

**Consumes:** every completed security boundary and pinned CLI contract. **Produces:** one supported default entrypoint, verifiable artifacts and explicit maintenance/login controls.

- [ ] Write failing config tests: no `latest` images/CLI installers; no host auth/project home mounts; daemon socket only in launcher; no credential env/build args; no DB/Redis/public controller ports; no extra providers/model-pull services; forbidden launch profiles and weak settings fail rather than boot insecurely.
- [ ] Add `test_unapproved_cli_contract_blocks`: changed CLI version/digest/tool contract must require the compatibility/security suite before startup is permitted. Verify the CLI binary and trusted policy/config hashes inside the actual artifact, not just a source constant.
- [ ] Build separate minimal images: controller without coding CLIs/provider SDKs, official Claude service without project toolchain, credential-free worker without login material, and pinned proxy. Pin base image digests, OS/package versions where supported, locked Python/npm deps and exact Claude artifact checksum. Record the selected architecture (Mac host may be arm64; verify native image/CLI support explicitly instead of reusing the earlier amd64 audit result).
- [ ] Build from a clean allowlisted context with no `.env`, `.git` credentials, host auth, SSH socket or package auth config. Fetch dependencies using maintenance only; project build scripts run offline and without credentials. Scan final images/layers/configs for synthetic secret sentinels and accidental credential files.
- [ ] Make `make personal-up`, `personal-status`, `personal-login`, `personal-check`, `personal-export` and non-destructive `personal-stop` the documented interface. Their exact target bodies must perform preflight and preserve the invariants. Login is owner-terminal-only; do not expose a token-returning HTTP endpoint. No automatic `down -v`, volume prune, auth deletion, account logout or unrelated cleanup.
- [ ] Remove unsafe upstream bootstrap/provider paths from this fork's supported default and CI release artifacts. A stale quickstart/Compose alias must refuse startup with a clear message, not launch the old insecure stack. Do not publish images to the upstream developer's registry or claim upstream artifacts contain these changes.
- [ ] Run deployment-policy tests, actual artifact inspection and full repository checks. Repair any directly affected old expectations. Commit reproducible deployment and setup instructions.

### Task 13 — Prove the full boundary with hostile fixtures and hand off the personal release

**Files:** `tests/security/subscription/test_acceptance.py`, synthetic fixture projects, `scripts/personal/verify_boundary.py`, `docs/personal/validation-report.md`, implementation ledger; no real credentials in committed fixtures.

**Consumes:** finished personal images/configs, pinned network/CLI policy and local workflow. **Produces:** an evidence-backed release verdict and reproducible operator handoff.

- [ ] Write an adversarial acceptance matrix before running it. A malicious fixture attempts to read all known auth paths, environment and `/proc`; escape with symlinks/races; call built-in tools; edit policy; forge IDs; reach the daemon/host; install dependencies; publish a PR; enable telemetry; fetch remote images; abuse a preview; upload through HTTP/DNS/IPv6; and force API/provider fallback through quota/auth/stream failure.
- [ ] Run the complete matrix in disposable namespaces with synthetic credential canaries mounted only in the trusted runtime. Check actual collector counters, worker filesystem results, mounts, process/network isolation, exported artifacts, DB/UI responses and logs. Do not accept a source grep, fake binary, mocked spawn or an empty string scan as the sole isolation proof.
- [ ] Exercise every role, including Intake/Secretary/background calls, plus a controller/runtime/launcher restart mid-job. Verify one active credential owner, expired/revoked capability denial, cancellation cleanup, idempotent writes and honest incomplete task states. Confirm no retry chooses a different model/auth/provider.
- [ ] Run all required format/lint/type/test/build gates in §12. Use fresh disposable test state and read hooks/targets first. Do not widen safety policies to get a green build.
- [ ] With the owner's dedicated subscription profile and paid extra usage disabled, run one small synthetic local project end to end. Confirm auth metadata and account usage indicate subscription operation, and that model-requested shell/file work occurred only in workers. A successful prompt alone does not prove no overage or no credential exposure.
- [ ] Save redacted evidence keyed by commit, CLI/image digests, architecture, policy hash, test commands and observed results. Explicitly separate tested synthetic behavior, live subscription behavior and anything not validated. No raw pcap/TLS dump, prompts, credentials or account identifiers in the shared report.
- [ ] Review all Global Constraints against the final diff and actual artifacts. Fix uncovered paths and repeat only the affected checks. Mark release blocked if any required boundary lacks evidence.
- [ ] Provide the owner a personal fork/PR URL if publication was authorized, check status, setup/login instructions, deliberate feature limitations and validation report. Do not merge/deploy on the owner's behalf without explicit instruction. Do not claim “cannot leak” beyond the threat model in §2.4.

## 11. Required negative-test matrix

These are release requirements, not optional future coverage.

| Test input/action | Expected result |
| --- | --- |
| Valid subscription plus any API-key/helper/custom endpoint configuration | Startup or dispatch denied, no value logged, zero API-key request |
| Console login / unrecognized auth-status schema / no login | `login_required` or `configuration_error`, no inference |
| Quota exhausted or chosen model unavailable | Park/fail same configured route; no alternate auth, model or provider |
| Project `.env`, private key, `.npmrc`, auth store, `.git` credential helper in snapshot | Excluded/rejected before admission; no automatic secret exception |
| Project `.claude/settings.json`, `.mcp.json`, `CLAUDE.md`, plugin, hook, auto-agent instructions | Cannot configure trusted CLI; untrusted text never grants authority |
| Model attempts Read/Bash/WebFetch/Task or asks for runtime home | Built-ins unavailable; gateway denies unauthorized operation |
| Quality script, install hook or test reads auth paths/env/proc | No provider/control credentials exist in its namespace/environment |
| Cross-project or stale run capability; replayed job | Denied server-side; no worker for that request |
| Symlink/hardlink/traversal/race/special file | Rejected without opening/writing outside workspace |
| Controller-side project shell/Git/package/test discovery attempt | No reachable implementation; all such work routed to offline worker |
| Worker HTTP/DNS/IPv6/host gateway/daemon access | Blocked by real network/mount boundary |
| Same approved domain, disallowed telemetry/share/update HTTP path | Denied by TLS-aware egress policy |
| SNI/Host mismatch, IP literal, redirect, DNS rebinding | Denied; no unapproved collector traffic |
| Proxy outage, bad/empty policy or CLI digest drift | Stack cannot dispatch a model run |
| Feature flag/API call enabling Guard Core, research, another provider, remote storage or sandbox | Policy error; no sender job/request |
| Unknown terminal event, partial stream then error, timeout/restart/cancel | Failed/incomplete/cancelled state; no false completion or repeated unacknowledged write |
| Public/forged dashboard request, CSRF, wrong Host, unauthenticated WebSocket | Denied; no standing owner credential injected |
| Remote image or project HTML preview in dashboard | No automatic external request or app-origin script execution |
| Export/log/error receives synthetic secret canary | Canary absent; deliberate task source is identified separately from credentials |
| Old Compose/quickstart alias | Explicit refusal or secure canonical profile; never starts an unrestricted legacy deployment |

## 12. Validation commands and evidence discipline

Task authors must run the smallest owning tests first. Suggested new test groups, once implemented:

```bash
uv run pytest tests/unit/subscription -q --no-cov
uv run pytest tests/integration/subscription -q --no-cov
uv run pytest tests/security/subscription -q --no-cov
```

Separate live tests with an explicit pytest marker/opt-in; ordinary test runs must not authenticate, spend subscription allowance, start real account logins, or reach public collectors. Tests that create/delete containers/DBs/volumes must restrict themselves to a uniquely named disposable test project and verify ownership before cleanup.

Existing backend canonical full gate is `make quality`, whose audited body includes Compose parity, formatting, Ruff, prose checking, Mypy, coverage >=75%, complexity/dead-code/security/dependency checks, Alembic SQL parsing, import boundaries and foundation checks. **Read the current body and prerequisites first**; it depends on `sync`, and other targets contain deletion commands. Where the owner's no-destructive-action rule requires it, run the underlying non-destructive commands against the disposable checkout/test state or obtain per-scope approval; never silently skip required checks. Do not run the whole suite against personal/shared databases.

Frontend location is `panel/`; use its pinned package manager. Required validation for changed frontend/deployment behavior:

```bash
pnpm --dir panel format:check
pnpm --dir panel lint
pnpm --dir panel typecheck
pnpm --dir panel test
pnpm --dir panel build
```

Dependency acquisition belongs to the separate credential-free maintenance environment. Builds also must not inherit the owner's personal provider environment. Check actual package scripts before execution.

Add `make personal-check` for static policy + synthetic runtime/worker/network checks and a separate explicitly opt-in live smoke target. The actual container security tests require Linux containers/Docker Desktop; a skipped host-incompatible suite is **not** a pass.

Every validation report records: commit SHA; OS/architecture; CLI and image digests; policy/config hashes; exact commands; actual pass/fail/skipped counts; fake versus live credentials; accepted unavailable features; and remaining threat-model limitations. Never equate an advisory count to exploitable bugs without reachability triage. Do not reuse older image results as proof for newly built images.

## 13. Startup and owner experience

Expected normal startup:

```text
make personal-up
  verify exact images/CLI/policy
  verify launcher, worker and network boundaries
  verify subscription authentication method
  refuse unsupported/unsafe configuration
  expose authenticated dashboard on 127.0.0.1:3000 only
```

Expected UI states: `Subscription ready`, `Login required`, `Subscription limit reached`, `Blocked by security policy`, `Configuration error`. Display the chosen model, queue state and task-level incomplete errors. Do not expose raw auth details, a key textbox, an “enable fallback” control, a hidden developer telemetry toggle or a “run anyway” isolation bypass.

`personal-login` prints instructions and runs the supported dedicated terminal login flow. It cannot be invoked by a task/model tool. `personal-stop` stops owned services while preserving credential/workspace volumes; deleting them or logging the account out is a separate explicit owner action.

For each task, the owner imports a sanitized snapshot, selects an allowed Claude model, reviews local changes/QA evidence and exports a patch. The project can contain malicious commands; the worker boundary remains in force even when the command is intentionally allowed.

## 14. Deliberately unavailable in the first secure version

- Other model providers or direct Anthropic API access.
- Provider API-key forms, custom model endpoints and custom token refresh daemons.
- Arbitrary web/research/browser tools, social publishing, Telegram and remote storage/sandboxes.
- Ollama/cloud generation and semantic/vector indexing/RAG until a separate approved local-only design exists.
- Automatic external forge clone/push/PR/watch operations; strict mode uses local review and owner export.
- Active project HTML/JS previews on the dashboard.
- Projects requiring source symlink/hardlink admission.
- Concurrent Claude sessions sharing one credential store, personal home/config mounts and privileged project scripts.

These are intentional policy constraints, not incomplete tasks to bypass later. The status/UI must state unavailable functionality rather than return empty success. Preserve ordinary task planning, coordination, file edits, offline tests, QA and local owner review.

## 15. Exit criteria and release blockers

Release is acceptable only when all are true:

- [ ] Exact official Claude artifact and subscription-only authentication contract proven.
- [ ] Owner has disabled paid extra/usage credits/top-ups; limitation and setup evidence stated accurately.
- [ ] Every model path uses the one protected subscription service; API keys and fallback routes are unreachable and rejected.
- [ ] No provider credential or personal auth mount reaches controller/model tools/project workers.
- [ ] Every project-controlled execution path runs offline in the worker; no Docker/control access from those paths.
- [ ] Role/run/workspace authorization is enforced server-side, including missing-manifest and restart/replay cases.
- [ ] Endpoint-level egress policy and actual namespace enforcement pass negative tests, including proxy loss and same-host telemetry.
- [ ] Dashboard/auth/browser resource/log/export boundaries pass adversarial tests.
- [ ] Personal local-review flow completes without forge credentials, fake PRs or false validation success.
- [ ] New images and every supported bootstrap path satisfy the same invariants; source-only checks are not substituted for artifact checks.
- [ ] Relevant regression tests plus complete required quality/frontend/build gates pass, with justified explicit skips never counted as acceptance.
- [ ] Redacted validation report and personal setup/handoff are complete; no real credential material in Git or exported artifacts.

Hard blockers include: unsupported subscription authentication, inability to suppress privileged built-in tools/config loading, unverified auth schema, paid-usage uncertainty falsely presented as guaranteed, privileged project execution, a raw token visible through a tool, a worker control/network escape, incomplete proxy enforcement, a callable direct API path, or a normal launch path that re-enables excluded behavior.

## 16. Primary references and verification date

Verified on 2026-11-20; recheck live provider behavior during Task 1 and before release. Vendor changes may require stopping for compatibility work; they do not authorize changing the owner's subscription-only requirement.

- [Claude subscription with Agent SDK / `claude -p`](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan): current official confirmation that signed-in use draws from subscription limits.
- [Claude authentication](https://code.claude.com/docs/en/authentication): supported login and platform credential storage. The Mac's personal Keychain is not a Linux container auth mount.
- [Claude CLI reference](https://code.claude.com/docs/en/cli-reference): candidate tool/config/stream/auth-status controls must be tested on the exact pinned build.
- [Claude environment controls](https://code.claude.com/docs/en/env-vars): nonessential traffic and related optional integrations; controls supplement the network boundary.
- [Claude data handling](https://code.claude.com/docs/en/data-usage): disabling analytics does not make hosted inference local or remove provider retention.
- [Separate paid usage credits](https://support.claude.com/en/articles/12429409-manage-usage-credits-for-paid-claude-plans): subscription login can coexist with paid continuation; account controls are essential to the no-extra-usage objective.
- [Audited upstream source](https://github.com/rennf93/roboco/tree/9d7747f65050f70489b26ec56b9df2d593bf4d87): actual implementation baseline, not a security endorsement.

## 17. Copy-paste handoff for the implementing session

> Implement this plan in my **personal RoboCo fork**, outside all company/FabricBloc repositories. Read the complete plan, the actual fork instructions and its owning code before editing. Scope: Claude subscription only, no API keys, no provider/model/auth fallback, no credentials accessible to model tools or project code. Keep the official Claude runtime trusted and separate from disposable offline project workers. Follow the numbered tasks and Global Constraints; use the execution skill if available. Use the existing `/Users/thesilentknight/Projects/Personal/roboco` checkout on `docs/security/claude-subscription-hardening`, with personal fork `PramodTKodag/roboco` as origin. Finish Task 0's remaining baseline work and Task 1's real compatibility proof before bulk edits; do not recreate the fork or checkout. Continue synthetic work while waiting for my dedicated subscription login setup. Do not read/copy/export my existing credentials or Keychain, enable paid usage, merge/deploy, delete volumes, or relax a security control to make tests pass. Record commands and bounded redacted evidence per task. If a required official CLI/auth/network capability cannot be enforced, identify the precise blocker; do not substitute API keys or call the fork secure. Deliver an implementation branch/PR, tests, exact image/CLI/policy pins, setup instructions and an honest validation report. Ask only for genuinely missing owner-specific information or scope-specific destructive approval.

## 18. Plan author self-review

- Requirement coverage: subscription-only auth → Tasks 1–2/7–8; credential/tool boundary → Tasks 3–6; local workflow → Task 9; network → Task 10; dashboard/log exposure → Task 11; reproducibility/alternate entrypoints → Task 12; actual proof → Task 13.
- Type/interface coverage: all inter-task objects and service methods are declared in §5; task payloads cannot carry arbitrary Docker/CLI configuration.
- Review Focus coverage: each of the five high-risk classes in §8 has named owning tests and actual acceptance coverage in §§10–11.
- No implementation claim: code paths/source baseline inspected; proposed new architecture, flags in combination, live auth, Mac/arm64 support and network policy still require their stated tests.
- Self-contained handoff: no memory, previous chat, audit temporary folder, undisclosed credentials or company documentation required.
