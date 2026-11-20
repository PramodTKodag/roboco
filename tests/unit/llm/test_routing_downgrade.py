"""ModelRoutingService never moves an agent to another provider on its own.

An agent with no assignment uses the default Anthropic route. An agent whose
configured provider is disabled, unreachable or unreadable raises
``ProviderRoutingError`` instead of silently running on a different provider.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from roboco.models.base import AssignmentScope, ModelProvider
from roboco.utils.crypto import EncryptionError
from roboco.services.llm import (
    ModelRoutingService,
    ProviderRoutingError,
    _ResolvedAssignment,
)

_AGENT_SLUG = "be-dev-1"


def _disabled_resolved() -> _ResolvedAssignment:
    provider = MagicMock(
        enabled=False, id="prov-disabled", type=ModelProvider.OLLAMA_CLOUD
    )
    return _ResolvedAssignment(
        provider=provider, model_name="grok-build", scope=AssignmentScope.GLOBAL
    )


def _svc() -> ModelRoutingService:
    svc = ModelRoutingService(MagicMock())
    # structlog's bound logger — swap for a mock so we can assert call sites.
    object.__setattr__(svc, "log", MagicMock())
    return svc


@pytest.mark.asyncio
async def test_disabled_configured_provider_is_refused_not_downgraded() -> None:
    svc = _svc()
    with (
        patch.object(
            svc, "_resolve_assignment", AsyncMock(return_value=_disabled_resolved())
        ),
        pytest.raises(ProviderRoutingError, match="disabled"),
    ):
        await svc.resolve_for_agent(_AGENT_SLUG)

    svc.log.error.assert_called_once()


@pytest.mark.asyncio
async def test_unreachable_self_hosted_server_is_refused_not_downgraded() -> None:
    provider = MagicMock(
        enabled=True,
        id="prov-local",
        type=ModelProvider.LOCAL,
        base_url="http://ollama.invalid:11434",
    )
    resolved = _ResolvedAssignment(
        provider=provider, model_name="llama", scope=AssignmentScope.GLOBAL
    )
    svc = _svc()
    with (
        patch.object(svc, "_resolve_assignment", AsyncMock(return_value=resolved)),
        patch(
            "roboco.services.llm.probe_ollama_tags",
            AsyncMock(return_value=(None, "connection refused")),
        ),
        pytest.raises(ProviderRoutingError, match="unreachable"),
    ):
        await svc.resolve_for_agent(_AGENT_SLUG)


@pytest.mark.asyncio
async def test_self_hosted_provider_without_base_url_is_refused() -> None:
    provider = MagicMock(
        enabled=True, id="prov-local", type=ModelProvider.LOCAL, base_url=None
    )
    resolved = _ResolvedAssignment(
        provider=provider, model_name="llama", scope=AssignmentScope.GLOBAL
    )
    svc = _svc()
    with (
        patch.object(svc, "_resolve_assignment", AsyncMock(return_value=resolved)),
        pytest.raises(ProviderRoutingError, match="no base_url"),
    ):
        await svc.resolve_for_agent(_AGENT_SLUG)


@pytest.mark.asyncio
async def test_undecryptable_provider_token_is_refused_not_downgraded() -> None:
    provider = MagicMock(enabled=True, id="prov-grok", type=ModelProvider.GROK)
    resolved = _ResolvedAssignment(
        provider=provider, model_name="grok-build", scope=AssignmentScope.GLOBAL
    )
    svc = _svc()
    with (
        patch.object(svc, "_resolve_assignment", AsyncMock(return_value=resolved)),
        patch.object(
            svc, "_route_from_assignment", AsyncMock(side_effect=EncryptionError("x"))
        ),
        pytest.raises(ProviderRoutingError, match="cannot be decrypted"),
    ):
        await svc.resolve_for_agent(_AGENT_SLUG)


@pytest.mark.asyncio
async def test_no_assignment_uses_the_default_anthropic_route() -> None:
    svc = _svc()
    with patch.object(svc, "_resolve_assignment", AsyncMock(return_value=None)):
        route = await svc.resolve_for_agent(_AGENT_SLUG)

    assert route.provider_type == ModelProvider.ANTHROPIC
    svc.log.error.assert_not_called()
    svc.log.warning.assert_not_called()


@pytest.mark.asyncio
async def test_haiku_lifecycle_assignment_upgraded_to_the_floor() -> None:
    """The structured-verb capability floor: a haiku-class Anthropic
    assignment on any lifecycle role resolves to sonnet instead, so a QA/PM
    agent can produce the structured review envelopes (2026-07-24 retirement).
    """
    provider = MagicMock(
        enabled=True, id="prov-anthropic", type=ModelProvider.ANTHROPIC
    )
    resolved = _ResolvedAssignment(
        provider=provider,
        model_name="claude-haiku-4-5-20251001",
        scope=AssignmentScope.ROLE,
    )
    svc = _svc()
    captured: dict[str, str] = {}

    async def _fake_route(res: _ResolvedAssignment, _slug: str) -> object:
        captured["model"] = res.model_name
        return object()

    with (
        patch.object(svc, "_resolve_assignment", AsyncMock(return_value=resolved)),
        patch.object(svc, "_route_from_resolved", _fake_route),
    ):
        await svc.resolve_for_agent("be-qa")

    assert captured["model"] == "sonnet"


@pytest.mark.asyncio
async def test_non_anthropic_below_floor_name_is_untouched() -> None:
    """The floor is an Anthropic-tier policy — a non-Anthropic model whose
    name happens to contain the marker is never upgraded."""
    provider = MagicMock(enabled=True, id="prov-grok", type=ModelProvider.GROK)
    resolved = _ResolvedAssignment(
        provider=provider, model_name="grok-haiku-ish", scope=AssignmentScope.ROLE
    )
    svc = _svc()
    captured: dict[str, str] = {}

    async def _fake_route(res: _ResolvedAssignment, _slug: str) -> object:
        captured["model"] = res.model_name
        return object()

    with (
        patch.object(svc, "_resolve_assignment", AsyncMock(return_value=resolved)),
        patch.object(svc, "_route_from_resolved", _fake_route),
    ):
        await svc.resolve_for_agent("be-qa")

    assert captured["model"] == "grok-haiku-ish"
