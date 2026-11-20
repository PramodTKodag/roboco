# Official Claude CLI contract (Task 1)

Evidence for the pinned official CLI. Everything here was observed against the real pinned binary inside locked-down containers (read-only root, all capabilities dropped, `no-new-privileges`, throwaway HOME, internal-only Docker network) talking to a **synthetic** provider. No real credential, no owner configuration, no Keychain and no public network were involved. Live subscription behavior is listed separately under open items.

## Pinned artifact

| Item | Value |
| --- | --- |
| CLI version | `2.1.311` (vendor `stable` dist-tag on 2026-11-20) |
| arm64 package (npm, sha512 SRI) | `sha512-yZwDmtXmoZcFcCjCaoznOKqjrjjdQyc6PVuh6brANqQcaKpP+WZGuFlgUjgQh86u8HBybHZBB60WWOumKpO6uQ==` |
| arm64 binary sha256 | `2e2496c9a8065df93acb0fca2d341f59a6b8777649b2e025ab8c9bcd5eef61f3` |
| x64 package (npm, sha512 SRI) | `sha512-dk1lVMt7n+NlYucKM7dLp/PfrK32pa/4oKQkacfazlSj8rG+hkH67wnDnaFG9Gs2al3OjK8PTVHgZ2WGHXoWUg==` |
| x64 binary sha256 | `86dc1fcc69498159eaf15f446613e847010f9036bfbbdffd4c49350be8489ada` (extracted and hashed; not executed, host is arm64) |
| Probe base image | `debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc` (arm64) |
| Fake provider image | `python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72` (arm64) |

Why 2.1.311: it is the vendor's `stable` tag and is newer than the patched version of each of the 15 most recent advisories on the vendor's security-advisory feed (checked 2026-11-20; highest patched version listed: 2.1.260). The newer `latest` (2.1.329) is one day old and was not chosen. The package is the vendor's platform package; its manifest declares no install scripts, so it is unpacked directly (only `package/claude`, regular-file check, size cap) and never installed through `npm`. `scripts/personal/probe_cli_contract.py stage` downloads, verifies both digests and refuses on any mismatch.

## Observed `claude auth status --json` schema

| Condition | `loggedIn` | `authMethod` | `apiProvider` | Extra | Exit |
| --- | --- | --- | --- | --- | --- |
| No login | `false` | `none` | `firstParty` | | 1 |
| Inherited `ANTHROPIC_API_KEY` | `true` | `api_key` | `firstParty` | `apiKeySource: "ANTHROPIC_API_KEY"` | 0 |

An inherited key makes the CLI report itself as logged in, so the policy must key on `authMethod` and the runtime environment must be built from an allowlist, never inherited. `classify_auth_status` accepts only `loggedIn=true, authMethod="claude.ai", apiProvider="firstParty"` with no `apiKeySource`, and treats every unrecognised value as `unknown_schema` (blocked).

## Verified controls (real binary, synthetic provider)

Argv under test (`build_probe_argv`): `-p --output-format stream-json --verbose --model <id> --tools "" --strict-mcp-config --mcp-config <trusted> --setting-sources "" --settings <trusted> --permission-mode dontAsk --permission-prompts none --no-session-persistence --disable-slash-commands`. Trusted settings: `{"permissions":{"allow":["mcp__probe__echo"]}}`. `--bare`, `bypassPermissions` and `auto` are never used.

| Claim | Result |
| --- | --- |
| Only the provisioned MCP tool is advertised to the model | Pass: the request's `tools` array and the init event both list exactly `mcp__probe__echo` |
| Built-in tool calls never execute (`Bash`, `Read`, `Write`, `Edit`, `Glob`, `Grep`, `WebFetch`, `WebSearch`, `Task`, `Agent`) | Pass: each attempted call returns an error tool result ("No such tool available ... disabled for this session, in subagents as well as here") |
| The allow-listed MCP tool runs under `dontAsk` | Pass: one `tools/call` reached the MCP server. In an earlier exploratory run with the default permission mode and no allow-list entry, the same call was not executed (not yet pinned by a test) |
| Hostile project (`.claude/settings.json` hooks, `.mcp.json`, `CLAUDE.md`, agents, base-URL override) cannot configure the CLI | Pass: no hook beacon and no hostile MCP contact; tool surface unchanged |
| The hostile fixture is effective (control) | Pass: with the isolation flags removed, hooks fire and the hostile MCP server is contacted. Without that, the inertness result would prove nothing |
| Inherited API key / non-subscription auth is refused by the classifier | Pass |
| Unknown auth schema is refused by the classifier | Pass (13 table cases) |

Test evidence: `tests/integration/subscription/test_cli_contract.py`: 36 passed, 0 skipped on 2026-11-20 (19 pure, 17 real-binary container tests). Command: `uv run pytest tests/integration/subscription/test_cli_contract.py -q --no-cov`. The container layer skips with an explicit reason if Docker or the staged binary is missing; a skip is not a pass.

## TLS-proxy feasibility spike (synthetic, 2026-11-20)

Setup: pinned CLI in a locked container on an internal-only network, `HTTPS_PROXY` pointing at a pinned `mitmproxy` 12.2.1 (`mitmproxy/mitmproxy@sha256:743b6cdc817211d64bc269f5defacca8d14e76e647fc474e5c7244dbcb645141`, arm64) with a private CA trusted through `NODE_EXTRA_CA_CERTS`, `connection_strategy=lazy`, and an addon that answered `/v1/messages` synthetically and returned 403 to every other request. The CLI's default base URL was used (no `ANTHROPIC_BASE_URL`).

| Finding | Result |
| --- | --- |
| The Bun-compiled CLI honors `HTTPS_PROXY` and trusts the private CA via `NODE_EXTRA_CA_CERTS` | Yes. A full prompt completed through the intercepting proxy |
| The proxy sees host, method and path of each request | Yes (`api.anthropic.com:443`), so path-level policy is enforceable |
| Same host carries telemetry next to inference | Confirmed. One run produced `POST /api/event_logging/v2/batch`, `POST /api/eval/...`, `GET /api/claude_cli/bootstrap`, `GET /api/claude_code_penguin_mode`, `GET /api/claude_code/organizations/metrics_enabled`, `GET /api/claude_code/policy_limits`, `GET /api/claude_code/settings` and `POST /v1/messages?beta=true`. A hostname allow-list cannot separate these |
| The CLI tolerates the proxy denying every non-inference path | Yes. The prompt completed with all other requests answered 403 |
| Effect of `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_TELEMETRY=1`, `DISABLE_ERROR_REPORTING=1` | Event logging, eval, bootstrap, penguin-mode and metrics calls disappear client-side. Only `policy_limits`, `settings` and `/v1/messages` remained. The proxy remains the enforcement point; the switches are supplement |
| mitmproxy needs `connection_strategy=lazy` on a network with no direct name resolution | Without it the proxy tried to resolve the origin at CONNECT time and returned 502 before any TLS |
| mitmproxy image entrypoint needs capabilities | Run `mitmdump` directly as a non-root user (`--entrypoint mitmdump --user 1000:1000 --set confdir=...`) with all capabilities dropped |

Not covered by the spike and still required in Task 10: OAuth endpoints (token refresh and any login-time destinations), which only a real login can reveal; redirects, SNI/Host mismatch, IP literals, DNS rebinding and IPv6 negative tests; proxy-death behavior; certificate verification of the real upstream.

CLI-internal retries: when the proxy was unreachable, the CLI emitted `api_retry` events with `max_retries: 10` and exponential backoff. The plan allows at most two retries on the same configuration, so Task 6 must find and test a control that bounds this (or enforce the bound by terminating the run); until then the CLI's own retry count is unbounded relative to the plan.

## Other observations that shape later tasks

- The init event lists builtin plugins, including `cc-plugin-telemetry`. Telemetry cannot be trusted to be off by flags alone: the endpoint-level egress policy (Task 10) is the control, with the CLI's nonessential-traffic switches as a supplement.
- Without the isolation flags and with an untrusted workspace, the CLI printed "Ignoring 1 permissions.allow entry from .claude/settings.json: this workspace has not been trusted". The workspace-trust gate is a vendor behavior and is not relied on.
- The CLI contacts the MCP endpoint (`server/discover`, `initialize`, `GET /mcp`, `tools/list`) before the first model request. The tool gateway must authenticate these connections as run-scoped.
- The synthetic provider used `ANTHROPIC_API_KEY` as a transport credential for the fake only, so these results prove the tool/config surface, not OAuth behavior.

## Open items (not yet proven)

1. **Live subscription login** (owner-dependent): confirm the exact `authMethod` value for a subscription login (`claude.ai` is the plan's expectation and is **unverified**), whether `apiKeySource` is absent, and that a benign tool-free prompt runs on subscription auth. Until then the classifier fails closed.
2. **Essential endpoint inventory for OAuth.** The TLS-proxy mechanism is feasible (see spike), but the authentication/refresh endpoints can only be captured with a real login. The strict network claim is not achieved until Task 10 enforces and tests the policy.
3. `--restricted` was not part of the verified argv. The tested combination already yields an empty built-in tool set; `--restricted` would add file-tool confinement and settings-file ignoring as extra depth and needs its own test before being added.
4. The x64 binary was hashed but not executed.
