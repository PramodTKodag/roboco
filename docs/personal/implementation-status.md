# Implementation status ledger

The security architecture in `claude-subscription-hardening-plan.md` is the **target**, not current behavior. Nothing below is claimed done unless a row cites evidence.

Legend for evidence: **synthetic** = fake credentials / fixtures; **container** = real Docker namespaces/network; **live** = real subscription login; **mocked** = unit-level fakes only.

## Environment (recorded 2026-11-20)

| Item | Value |
| --- | --- |
| Checkout | `/Users/thesilentknight/Projects/Personal/roboco`, branch `docs/security/claude-subscription-hardening` |
| Remotes | `origin` = `PramodTKodag/roboco`, `upstream` = `rennf93/roboco` |
| Upstream drift | none: `upstream/master` is still `9d7747f65050f70489b26ec56b9df2d593bf4d87` (plan baseline) |
| Host | macOS, arm64 (aarch64) |
| Container runtime | **OrbStack** 29.8.2 (aarch64), not Docker Desktop as the plan assumed. Network/firewall enforcement in Task 10 must be proven on this runtime. |
| Python / uv | project `requires-python >=3.13,<3.15`; venv 3.14.2; uv 0.10.11 |
| Node / pnpm | 24.15.0 / 10.33.0 |
| Git hooks | none installed in `.git/hooks` (a `.pre-commit-config.yaml` exists but is not installed) |
| Test DB | disposable container `roboco-sub-test-pg` (postgres:15.4-alpine) on `127.0.0.1:15432`, label `roboco.personal.test=1`. The repo's `conftest.py` default points here. Never the owner's other Postgres containers. |
| Host port 3000 | already bound by an unrelated container (`fabricbloc-grafana`). Plan's `127.0.0.1:3000` publish conflicts; handled in Tasks 11-12 (configurable loopback port). |
| Unrelated containers | many `jlpt-*` and `fabric-*` containers are running. Out of scope; must never be touched by cleanup. |

Destructive Makefile targets that must **not** be run: `test` (`down --rmi all -v` + `docker system prune`), `prune`, `clean`, `install`/`install-dev`/`lock`/`upgrade` (recursive `rm -rf` of cache dirs via `find | xargs rm -rf`), `reset_runtime_state.*`. Gates are run through the underlying non-destructive commands (`uv run ruff/mypy/pytest/...`), as `quality` itself is non-destructive but depends on `sync`.

## Baseline (Task 0)

Existing tests relevant to routing / quality gate / auth / provider routes / video, run against the disposable DB:

```
uv run pytest tests/unit/llm tests/unit/runtime/test_provider_routing.py \
  tests/unit/runtime/test_spawn_strict_mcp.py tests/unit/runtime/test_interactive_provider_guard.py \
  tests/unit/gateway/test_quality_gate.py tests/unit/api/auth tests/unit/api/test_websocket_auth.py \
  tests/integration/test_llm_routing.py tests/integration/test_provider_routes.py \
  tests/integration/test_video_routes.py -q --no-cov
=> 782 passed, 3 warnings (0 failed, 0 skipped)
```

Result is green on the *insecure* upstream behavior, so these tests encode the multi-provider contract that Tasks 7-8 intentionally replace.

### Read-only inventory (path level; no config values read)

- **Model clients / provider routing** (top files by match count): `roboco/services/llm.py`, `roboco/api/routes/provider.py`, `roboco/runtime/engines/spawn_launch.py`, `roboco/services/optimal_brain/{shared_embedder,ollama_embedder,indexer_worker,mentor,validator}.py`, `roboco/llm/providers/*` (openrouter, nebius, hummin, kimi, grok, gemini, codex), `roboco/billing/pricing.py`, `roboco/runtime/orchestrator.py`, `roboco/runtime/engines/{interactive_sessions,spawn_exit,rate_limit_probe}.py`, `roboco/services/{optimal,x_engine,video_engine,vault_intake_engine,memory_distiller,extraction,tg_cockpit}.py`, `roboco/eval/runner.py`, `roboco/security.py`, `scripts/{bootstrap.sh,reset_runtime_state.sh,deploy-nas.sh}`, `docker/scripts/entrypoints/{openrouter,nebius}-agent-entrypoint.sh`.
- **Subprocess sites** (controller-side): `roboco/services/workspace.py` (33), `runtime/engines/spawn_exit.py` (16), `spawn_launch.py` (12), `sweeps.py` (10), `services/gateway/content_actions.py` (9), `services/git.py` (8), `services/release_executor.py` (7), `runtime/engines/{reconcile,dispatch_breaker}.py` (6 each), `llm/providers/_docker.py` (6), `services/gateway/quality_gate.py` (3), `runtime/sandbox.py`, `agent_sdk/{live,grok}_cli_session.py`, `services/{conventions,release_readiness,release_manager_engine}.py`, `services/gateway/choreographer/evidence_legs.py`, `eval/runner.py`.
- **Credential mounts / Docker socket**: `docker-compose.yml` and `docker-compose.registry.yml` mount `/var/run/docker.sock` and `${CLAUDE_AUTH_DIR:-$HOME/.claude}:/root/.claude` into the orchestrator, plus host `.grok/.codex/.gemini/.kimi-code` dirs; `ROBOCO_HOST_*_DIR` env wiring; `roboco/runtime/engines/spawn_launch.py` and `spawn_config.py` build the per-agent mounts. Compose publishes `15432` (Postgres), `16379` (Redis), `19000/19001` (RustFS), `11435` (Ollama), orchestrator debug ports and nginx `3000:80`.
- **Alternate launch paths**: `docker-compose.yml`, `docker-compose.yaml` (byte-identical twin, enforced by `make compose-sync`), `docker-compose.registry.yml`, `scripts/bootstrap.sh` (`make quickstart`), `scripts/deploy-nas.sh`. All must refuse or be superseded by the personal profile (Task 12).
- **Panel external resource loads**: `panel/src/app/(tg)/layout.tsx` loads `https://telegram.org/js/telegram-web-app.js`; outbound `href`s to x.com / GitHub are owner clicks, not automatic loads. Markdown renderer (`panel/src/components/ui/markdown.tsx`) must be checked for remote images in Task 11.
- **nginx**: `deploy/nginx.conf` (token injection to be verified/removed in Task 11).

## Task ledger

| Task | Status | Commit | Evidence / blockers |
| --- | --- | --- | --- |
| 0 Baseline | done | 4eb4f66c | baseline above; fork verified |
| 1 CLI contract proof | **partial**: synthetic layer proven | 2af3082b, f255151b | 36 tests pass against the real pinned binary (container, synthetic provider). TLS-proxy mechanism proven feasible on synthetic traffic (same-host telemetry confirmed; path-level policy required). Open: live subscription login (authMethod value), OAuth endpoint inventory, bound on CLI-internal retries (Task 6). See `cli-contract.md`. |
| 2 Policy + status | **done (synthetic/mocked)** | 29f0cb9f | 64 unit tests in `tests/unit/subscription`: allowlisted env, prefix-based inherited-override rejection (names only), API-key/flag rejection at CLI start + API lifespan, DB rows cannot enable forbidden flags, redacted status via a stand-in CLI script, frozen bounded contracts. Gates run: ruff, mypy, xenon, radon, vulture, bandit, lint-imports, foundation checks. Scope notes: worker contracts (`WorkerJob`/`WorkerResult`) are defined in Task 3/4/5 where first consumed. Model allowlist IDs are unverified until a live run. Billing acknowledgement is a local timestamp, not proof. |
| 3 Launcher | **done (mocked + container)** | 04a152c1 | Typed newline-framed protocol with two distinct bearer tokens (admin registers/cancels, executor executes); server-side workspace/run registries with TTL, replay and cancel checks; fixed worker argv derived only from registered UUIDs. Worker image `docker/task-worker.Dockerfile` (pinned base digest, hash-pinned deps generated from `uv.lock`, import closure of the entrypoint enforced by test, user 10001, no CLI/git/docker). 7 real-Docker tests (`tests/integration/subscription/test_launcher.py`) inspect a running worker: network none, ipc none, read-only root, cap-drop ALL, no-new-privileges, pids 256, 2g memory/swap, 2 CPUs, one volume at `/workspace`, no binds or socket; also no outbound connect, workspace isolation, hard-timeout kill, cancel kill, and reconcile that touches only labelled workers. 152 unit tests in `tests/unit/subscription`. Gates run: ruff, mypy, bandit, xenon, vulture, lint-imports. Not proven: `runsc` (gVisor) is accepted by policy but is not installed on this OrbStack engine, so it is untested; The launcher process (`launcher_main.py`) is unit-tested for configuration only; it is wired into compose in Task 12. |
| 4 Workspace admission + files | **done (mocked + container)** | feaa4c71 | Import is owner-operated and offline: `scripts/personal/import_workspace.py` snapshots a directory (`roboco/runtime/workspace_snapshot.py`: lstat/no-follow, links and special files omitted, fixed deny rules, size caps) and sends an uncompressed tar through the admin-only launcher action `import_workspace` (body is never read before the admin token is verified). The archive is parsed only inside the offline worker (`workspace_admission.py`): symlink/hardlink/device/FIFO entries, traversal, absolute, encoded-traversal, duplicate/case-colliding names and size/entry overruns refuse the whole archive; denied credential/config paths are excluded and reported; suspected secrets (rule names and paths only, never values) refuse the import until each exact `PATH:RULE` is acknowledged. A failed import removes its volume; an existing volume is never overwritten or removed. File operations (`read/list/search/write`) use descriptor-relative no-follow access and refuse links, hardlinks, non-regular files and denied paths; `apply_patch` validates paths and modes through `git apply --numstat/--summary` first; `export_patch` fails closed on a section/path mismatch. Git runs with a clean environment and hooks/fsmonitor/pager/protocols disabled. 308 unit tests in `tests/unit/subscription` (hostile archives, hostile source trees, thread-based parent-swap races); 49 real-Docker tests in `tests/integration/subscription` and `tests/security/subscription/test_filesystem_isolation.py` (sibling-workspace sentinel, host marker, `/proc` environment, capabilities, planted symlinks/hardlinks, denied-path and credential patch smuggling). Intentional v1 limits: symlinks are never imported (a project that needs them is adjusted by the owner); git inside the worker is installed from the distribution archive of the pinned base image, so the apt packages are not hash-pinned; project code can rewrite `.git` inside its own sandbox, so the exported patch is untrusted until reviewed (Task 9); `runsc` remains untested on this engine. |
| 5 Project execution out of controller | **done (mocked + container); host-clone git remains** | uncommitted until the final gate run | Quality gate, project commands, conventions check and codegen no longer run on the controller. `roboco/runtime/subscription/project_executor.py` is the seam (`CommandRunner`, `ConventionsChecker`, `ProjectExecutor`): infrastructure failures raise `ProjectExecutionUnavailable(reason_code)` and block the submit, a command timeout is a failed command (exit 124), only an empty command list is a skip. The default provider is fail-closed (`worker_not_configured`) until Tasks 7/9 bind a project to a workspace and run. New worker operation `check_conventions` (tree-sitter + pyyaml added to the pinned worker requirements; a missing `yaml` import was caught by the real-container tests and fixed). Removed from the controller: dependency install, toolchain smoke test, venv linking, toolchain guard, codegen, and the dep-update upgrade probe (the engine now refuses and logs; deleted in Task 8). 6 obsolete test files deleted with owner approval. Evidence: unit suite 10293 passed, 4 failed (all environmental and pre-existing: 3 pgvector HNSW tests, 1 cloud-auth OAuth form test against the disposable DB); real-Docker hostile-project tests in `tests/security/subscription/test_project_execution.py` (project Makefile/build backend printing environment, reading credential paths, scanning every `/proc/*/environ`, reaching the host, spawning children: synthetic controller env and host-file secrets never appear; no network; unavailable worker raises instead of passing; missing toolchain fails the gate with a hint; conventions run in the built image and an unparseable conventions file is could-not-run, never a clean pass); static scan `tests/security/subscription/test_process_spawn_boundary.py` allowlists every remaining process-spawn site by category. Gates run: ruff check/format, mypy (`roboco tests`), xenon, vulture, bandit (no medium/high), deptry, lint-imports, reflow. Remaining by design (allowlisted, must shrink to zero): host-clone `git` sites in `workspace.py`, `git.py`, `content_actions.py`, `release_executor.py`, `release_readiness.py`, `conventions.py` (Tasks 7/9) and legacy provider runtimes (Task 7). Limits: the worker image has no project toolchains, so quality commands exit 127 until a locked pre-fetched toolchain exists; codegen is skipped (logged) pending the worker patch flow in Task 9; `toolchain_match_enabled` still drives a legacy production-credentials gate-env injection in `spawn_launch.py` and `roboco/services/toolchain.py` is unused (both Task 7/8); `docs/map/_complete_map.md` and `_front.md` are generated and still describe the dep-update probe. |
| 6 Claude service + MCP tools | not started | — | — |
| 7 Route all roles | not started | — | — |
| 8 Background/optional outbound | not started | — | — |
| 9 Local review | not started | — | — |
| 10 Egress | not started | — | — |
| 11 UI/auth/logs | not started | — | — |
| 12 Deployment | not started | — | — |
| 13 Acceptance + report | not started | — | — |

## Owner decisions that change the plan

- **2026-11-20, provider scope (supersedes "Claude only").** Keep the CLI-based providers (Claude Code, Codex, Gemini, Grok, Kimi) plus Nebius. Remove the rest in Task 7. Hummin ships a CLI wrapper and OpenRouter is API-key based: the owner has not yet ruled on Hummin, so it is treated as removed unless confirmed.
- **Nebius is an explicit, temporary exception to "no API keys".** The owner holds a Nebius API key on a free-token allowance and will disable the provider when the allowance ends. Required controls: the key is readable only by the trusted model service (never by workers, project code or model tools), the egress allowlist names only the Nebius host, no fallback to or from other providers, and a provider switch that is off by default. Kimi is kept in the code but disabled (off by default, not routable) until the owner re-enables it. Each added CLI provider needs its own contract proof (auth classification, retry bound, endpoint inventory) before it is enabled; none is enabled until then.

## Known environment limitations (not caused by this work)

- Disposable test Postgres (`postgres:15.4-alpine`) has no `pgvector` and rejects the `roboco` password the cloud-auth login test expects: 4 existing tests fail identically with and without these changes (`test_vector_store_hnsw_index` x3, `test_cloud_auth::test_login_route_parses_oauth2_form_not_query_params`). A pgvector-enabled disposable image is needed for a fully green suite.
- The developer shell exports provider/Claude variables, which the new startup policy correctly rejects; `tests/conftest.py` scrubs them for tests only.
- `roboco.runtime` now exports its names lazily, so importing `roboco.runtime.subscription.*` no longer loads the orchestrator (tested). The credential-bearing service image in Task 6 must still keep its import surface minimal and copy only its import closure, as the worker image does.

## Owner-dependent items

- [ ] Dedicated Linux-profile subscription login (needed only for Task 1 live check, Task 10 live capture, Task 13 live smoke).
- [ ] Owner disables paid extra usage / auto top-ups on the account (cannot be verified by software).
