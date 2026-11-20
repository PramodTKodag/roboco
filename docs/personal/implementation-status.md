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
| 2 Policy + status | **done (synthetic/mocked)** | (this commit) | 64 unit tests in `tests/unit/subscription`: allowlisted env, prefix-based inherited-override rejection (names only), API-key/flag rejection at CLI start + API lifespan, DB rows cannot enable forbidden flags, redacted status via a stand-in CLI script, frozen bounded contracts. Gates run: ruff, mypy, xenon, radon, vulture, bandit, lint-imports, foundation checks. Scope notes: worker contracts (`WorkerJob`/`WorkerResult`) are defined in Task 3/4/5 where first consumed. Model allowlist IDs are unverified until a live run. Billing acknowledgement is a local timestamp, not proof. |
| 3 Launcher | not started | — | — |
| 4 Workspace admission + files | not started | — | — |
| 5 Project execution out of controller | not started | — | — |
| 6 Claude service + MCP tools | not started | — | — |
| 7 Route all roles | not started | — | — |
| 8 Background/optional outbound | not started | — | — |
| 9 Local review | not started | — | — |
| 10 Egress | not started | — | — |
| 11 UI/auth/logs | not started | — | — |
| 12 Deployment | not started | — | — |
| 13 Acceptance + report | not started | — | — |

## Known environment limitations (not caused by this work)

- Disposable test Postgres (`postgres:15.4-alpine`) has no `pgvector` and rejects the `roboco` password the cloud-auth login test expects: 4 existing tests fail identically with and without these changes (`test_vector_store_hnsw_index` x3, `test_cloud_auth::test_login_route_parses_oauth2_form_not_query_params`). A pgvector-enabled disposable image is needed for a fully green suite.
- The developer shell exports provider/Claude variables, which the new startup policy correctly rejects; `tests/conftest.py` scrubs them for tests only.
- `roboco.runtime.__init__` imports the orchestrator, so importing `roboco.runtime.subscription.*` pulls the full application. Task 6 must keep the credential-bearing service image's import surface minimal (or move the service entry point out of that package).

## Owner-dependent items

- [ ] Dedicated Linux-profile subscription login (needed only for Task 1 live check, Task 10 live capture, Task 13 live smoke).
- [ ] Owner disables paid extra usage / auto top-ups on the account (cannot be verified by software).
