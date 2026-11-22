# Validation report: subscription-only personal deployment

This report separates what was tested with synthetic data, what ran in containers, and what has not been validated. Nothing here was run with the owner's real subscription login, so no claim below covers live subscription behavior.

## Release verdict

**Not released to live use.** The synthetic and container boundaries are proven on the evidence below. Three things only the owner can complete are still open: the dedicated login (with approval of the maintenance endpoints), one benign inference through the final egress policy, and a live end-to-end run. Until those are done, "model work uses only the subscription login" is proven for the code paths and the network boundary, not for a real account.

## Identity of what was tested

| Item | Value |
| --- | --- |
| Code revision | `076eddd0` (the tested tree; the commit that adds this report changes documentation only) on branch `docs/security/claude-subscription-hardening` (nothing pushed) |
| Audited baseline | `9d7747f65050f70489b26ec56b9df2d593bf4d87` |
| Host | macOS, arm64, OrbStack (Docker engine) |
| Pinned CLI | official CLI `2.1.311`, arm64 binary sha256 `2e2496c9a8065df93acb0fca2d341f59a6b8777649b2e025ab8c9bcd5eef61f3` (see `cli-contract.md`) |
| Egress policy digest | `fe8bb7cd260a245bcc64f3d0e38c4f640d309be3a001cc59b0c57be25feec1c9` (`deploy/personal/egress-policy.sha256`) |
| Proxy image | `mitmproxy/mitmproxy@sha256:743b6cdc817211d64bc269f5defacca8d14e76e647fc474e5c7244dbcb645141` |
| Database / cache / edge images | `pgvector/pgvector:pg16@sha256:484e838076e04939d27d3e176e91c86eea96236126023be8d3059761122e579f`, `redis:8-alpine@sha256:7958cfbb9d152f87dadfbc89f1b0b15938f0b81ff8982feb327476f4911935c3`, `nginx:1.29-alpine@sha256:5616878291a2eed594aee8db4dade5878cf7edcb475e59193904b198d9b830de` |
| Built images (tag `e36483ce7a08`) | controller `cb2558365a53`, launcher `b8ad4afc3d34`, model service `213d58941f73`, worker `8a014b5cc0e0`, panel `705e6ad997aa` (local image ids) |

The built images were produced from commit `e36483ce`. Later commits changed only tests and `pyproject.toml` tooling settings, no product code.

## 1. Synthetic and unit results

All data in these tests is synthetic. Credential canaries are fake values.

| Gate | Result |
| --- | --- |
| Full test suite (`pytest tests/`, pgvector test database) | 18,704 passed, 0 failed, 506 skipped (23 min) |
| Coverage | 90.87% (required: 75%) |
| `ruff format --check`, `ruff check` | pass |
| `mypy roboco/ tests/` | pass (1760 source files) |
| `xenon --max-absolute B --max-modules A --max-average A` | pass |
| `vulture --min-confidence 100`, `bandit -ll`, `deptry`, `lint-imports`, `pip-audit` | pass |
| Markdown reflow check, compose sync, `make foundation-check` | pass |
| Panel `typecheck`, `lint`, `vitest` | pass (the 5 panel files this work changed are Prettier-clean; older files have pre-existing drift, see limitations) |
| Skipped tests | 506 in the full run. Skips are never counted as acceptance; reasons are not itemised here (`pytest -rs` lists them) |

Commands: `env -i PATH=... HOME=... ROBOCO_TEST_DB_PORT=15433 ROBOCO_DATABASE_PORT=15433 python -m pytest tests/ --cov=roboco --cov-fail-under=75 -p no:cacheprovider -q`, and the individual tools above exactly as in the repository `Makefile` `quality` target. The tests ran from a shell with no provider credentials (`env -i`).

### Acceptance matrix

`tests/security/subscription/acceptance_matrix.py` maps each of the 20 required negative-test rows to named tests, and `test_acceptance.py` fails if a row has no evidence or names a test that no longer exists. All 20 rows are covered:

1. API key, helper or custom endpoint is refused
2. Console login, unknown auth schema or no login blocks inference
3. Quota or model failure never falls back
4. Credential files never enter a workspace
5. Project config cannot configure the trusted CLI
6. Built-in tools and ungranted operations are unavailable
7. Project scripts find no credentials in their namespace
8. Stale, cross-project or replayed capability is denied
9. Links, traversal and special files are refused
10. No project command runs on the controller
11. Worker has no network, daemon or host access
12. Same-host telemetry paths are denied
13. SNI, Host, IP, redirect and rebinding tricks are denied
14. Proxy outage, bad policy or CLI drift stops model runs
15. Forbidden features cannot be enabled
16. Partial, timeout, cancel and restart never report false completion
17. Forged, public, CSRF, wrong-Host or anonymous WebSocket requests are denied
18. Remote resources and project HTML cannot load or run (response policy only)
19. Secret canaries never reach logs, errors or exports
20. Legacy compose and quickstart refuse to start

## 2. Container results (OrbStack, real images)

| Check | Result |
| --- | --- |
| `tests/security/subscription` (real containers: workers, proxy, edge, filesystem isolation) | 321 passed |
| `tests/integration/subscription` (launcher, CLI contract against the real pinned binary, workspace import, local review) | passed in the last runs on this tree |
| Live stack boundary verifier (`scripts/personal/verify_boundary.py`) against the running stack | 23 of 23 checks pass |

The verifier inspects the running containers: no privileged or host-namespace container, all capabilities dropped, `no-new-privileges`, the docker socket only in the launcher, the credential volume only in the model service, only the edge publishes a port (loopback only), only the proxy joins the internet network, and the controllers and dispatcher have no direct route or name resolution. Redacted result: `.personal/verify-boundary.json` (private state, not committed).

Egress is enforced by the pinned proxy plus network topology, not by hostname or environment settings alone. Only `POST api.anthropic.com/v1/messages` is allowed in the normal profile.

## 3. Live subscription behavior

**Not validated.** Specifically:

- The dedicated Linux-profile login (`make personal-login`) has not been run, so the live `authMethod` value and the vendor OAuth endpoint list are unverified and the maintenance profile is still empty.
- No inference has gone through the final egress policy.
- No end-to-end task has run on the subscription, and account usage and overage were not inspected.

## 4. Not proven by code

These are stated in `NOT_PROVEN_BY_CODE` and cannot be closed by tests:

- A controller, runtime or launcher restart in the middle of a real model job (restart handling is tested with synthetic jobs only).
- Project HTML or remote images in a real browser (only the response policy and the CSP headers are checked).
- A full run through the owner's live subscription login.
- That paid extra usage is off: the CLI reports the sign-in method, not account billing. The owner must turn it off (`owner-setup.md`).

## 5. Owner actions still required

1. Turn off paid extra usage and auto top-ups on the account used for the dedicated profile.
2. Run `make personal-login` from a clean terminal, review the refused endpoints it prints, approve only what the official sign-in needs, update `egress-policy.sha256`, and repeat until `make personal-status` no longer shows `login_required`.
3. Run one small synthetic project end to end, then check account usage.
4. Open the panel in a real browser and check project markdown and previews.

## 6. Limitations

Security-relevant:

- Imported project files are sent to Anthropic for the model to read. Nothing here changes that.
- The controller secrets are environment values in a gitignored owner-only file and are visible through `docker inspect` to anyone who can run docker on this machine.
- The launcher runs as root so it can use the docker socket; it is reachable only from the controllers on an internal network with two distinct bearer tokens.
- The run token travels in a cleartext internal TCP frame behind a service token on the internal network.
- Redis has no password and relies on the internal data network. The postgres and redis containers are not read-only.
- CSP keeps `script-src 'unsafe-inline'` because of the Next.js inline bootstrap.
- The access guard is armed only when cloud auth is enabled. `127.0.0.1:8000` is in the allowed hosts.
- Safari rejects `Secure` cookies on `http://localhost`; use Chrome, Firefox or Edge.
- gVisor (`runsc`) and Docker Desktop are untested. OrbStack is the verified runtime.
- Suspected-secret patches are blocked, with no owner-acknowledge flow.
- Host-clone git helpers still exist in the controller and are listed in the spawn-boundary allowlist as removal-pending. Removing them deletes existing code and is awaiting owner approval. The legacy provider runtime files (Codex, Gemini, Grok, Kimi, Nebius) are in the same list and stay disabled by default.

Functional:

- Local review replaces pull requests: no push, PR or forge action is possible. Review is a patch of at most 64 KiB per task.
- One shared workspace volume per project, so concurrent tasks' changes mix.
- The worker image has no project toolchains, so project test and build commands that need one cannot run.
- Semantic features that need a model or network outside the subscription path are intentionally unavailable (knowledge base, RAG, mentor, validator, research, X, video, Telegram, forge automation, Decisions, vault, org memory). Their panel pages remain and refuse writes.
- Chat transcripts are in memory only. The run queue has no interactive priority.
- Stored routing presets that reference removed providers fail closed.
- The Anthropic recovery probe after a parking event is time-expiry only.
- A manifest verb that a flow server does not implement triggers a strict tool-surface block.
- `RunSpec.task_id` is None for delivery runs.
- Generated docs and the codebase map are stale.
- Panel files other than the five changed here have pre-existing Prettier drift and a few pre-existing lint warnings.

## 7. Reproducing

```text
make personal-stage     # fetch and verify the pinned CLI
make personal-up        # build the pinned images and start the stack
make personal-status
make personal-check     # security, container and policy suites
python scripts/personal/verify_boundary.py   # live stack boundary verifier
```

Run all of them from a shell with no provider credentials. The commands and the first-run flow are in `owner-setup.md`.
