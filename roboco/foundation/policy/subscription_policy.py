"""roboco/foundation/policy/subscription_policy.py

Pure rules for the subscription-only model policy: which authentication the
official CLI may report, which environment names and feature flags are
forbidden, and which exact model IDs may run. Stdlib only, no IO, so the
settings service, the runtime and the tests all share one source of truth.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

# `claude auth status --json` values. The subscription method string is the
# vendor's documented value and stays UNVERIFIED against a live login until the
# owner login checkpoint; every other value blocks dispatch.
SUBSCRIPTION_AUTH_METHOD = "claude.ai"
FIRST_PARTY_PROVIDER = "firstParty"

# Exact full model IDs only. Aliases ("sonnet", "latest") can silently select a
# different model and are never accepted. Owner-maintained, pinned together
# with the CLI contract.
ALLOWED_MODEL_IDS: frozenset[str] = frozenset(
    {
        "claude-fable-5-2",
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
    }
)

# Environment the trusted launcher itself sets for the official CLI. Presence
# of these names in controller input is harmless; their values are fixed by
# `build_claude_env`, never taken from the input.
TRUSTED_CLAUDE_ENV_NAMES: frozenset[str] = frozenset(
    {
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "DISABLE_TELEMETRY",
        "DISABLE_ERROR_REPORTING",
        "DISABLE_AUTOUPDATER",
        "CLAUDE_CODE_MAX_RETRIES",
    }
)

# A prefix rule, not a denylist of known names: the CLI reads hundreds of
# `ANTHROPIC_*` / `CLAUDE_CODE_*` variables (keys, tokens, gateways, base URLs,
# cloud-provider switches) and new ones appear with each release.
FORBIDDEN_ENV_PREFIXES: tuple[str, ...] = (
    "ANTHROPIC_",
    "CLAUDE_CODE_",
    "AWS_BEARER_TOKEN",
    "ROBOCO_HOST_",
)
FORBIDDEN_ENV_NAMES: frozenset[str] = frozenset(
    {
        "CLAUDE_AUTH_DIR",
        "ROBOCO_ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "XAI_API_KEY",
        "GROK_API_KEY",
        "MOONSHOT_API_KEY",
        "NEBIUS_API_KEY",
    }
)

# Features that send model or project traffic anywhere except the subscription
# runtime: a second model provider, a direct local-model client, a third-party
# service, or an unattended forge/social integration. They can be neither
# configured on nor persisted on; the code stays, the switch is locked.
FORBIDDEN_FEATURE_FLAGS: tuple[str, ...] = (
    # second model provider / remote execution
    "token_factory_sandboxes_enabled",
    "cross_vendor_review_enabled",
    # direct local-model clients (embeddings, drafting, classification)
    "direct_model_clients_enabled",
    "semantic_kb_enabled",
    "rag_auto_update_enabled",
    "indexer_stream_enabled",
    "decisions_enabled",
    "org_memory_enabled",
    "vault_intake_enabled",
    "vault_kb_enabled",
    # third-party services
    "research_enabled",
    "provisioning_enabled",
    "x_engine_enabled",
    "x_replies_enabled",
    "x_feature_spotlight_enabled",
    "video_engine_enabled",
    "telegram_enabled",
    "telegram_inbound_enabled",
    "telegram_miniapp_enabled",
    # unattended forge integrations
    "external_pr_enabled",
    "internal_pr_enabled",
    "self_heal_enabled",
    "self_heal_originate_enabled",
    "ci_watch_enabled",
    "dep_update_enabled",
    "env_sync_enabled",
    "release_manager_enabled",
    "docs_sync_enabled",
)


def forbidden_env_names(environ: Mapping[str, str]) -> list[str]:
    """Names (never values) of inherited auth/provider overrides, sorted."""
    return sorted(
        name
        for name in environ
        if name not in TRUSTED_CLAUDE_ENV_NAMES
        and (name in FORBIDDEN_ENV_NAMES or name.startswith(FORBIDDEN_ENV_PREFIXES))
    )


def enabled_forbidden_flags(flags: Iterable[tuple[str, bool]]) -> list[str]:
    """Forbidden feature flags that are switched on, in declaration order."""
    enabled = {name for name, value in flags if value}
    return [name for name in FORBIDDEN_FEATURE_FLAGS if name in enabled]


def _classify_logged_in_method(method: str, payload: dict[str, Any]) -> str:
    if method == "api_key":
        return "rejected_auth_method"
    if method != SUBSCRIPTION_AUTH_METHOD:
        return "unknown_schema"
    return "rejected_mixed_auth" if "apiKeySource" in payload else "subscription"


def classify_auth_status(payload: dict[str, Any]) -> str:
    """Classify `claude auth status --json`; anything unrecognised blocks dispatch."""
    logged_in = payload.get("loggedIn")
    method = payload.get("authMethod")
    provider = payload.get("apiProvider")
    if (
        not isinstance(logged_in, bool)
        or not isinstance(method, str)
        or not isinstance(provider, str)
    ):
        return "unknown_schema"
    if provider != FIRST_PARTY_PROVIDER:
        return "rejected_provider"
    if not logged_in:
        return "login_required" if method == "none" else "unknown_schema"
    return _classify_logged_in_method(method, payload)


def classify_auth_status_text(text: str) -> str:
    """Classify raw status output; unparseable or non-object output blocks."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return "unknown_schema"
    return (
        classify_auth_status(payload) if isinstance(payload, dict) else "unknown_schema"
    )
