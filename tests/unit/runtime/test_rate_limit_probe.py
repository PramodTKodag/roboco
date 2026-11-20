"""Rate-limit recovery probe — real per-provider liveness check.

``_do_probe`` replaced a time-based stub that always returned True. It now
makes a free, unmetered call (Anthropic ``GET /v1/models`` / Ollama
``GET /api/tags``) and treats only a 2xx response as the provider having
recovered — a 429 (rate limit) and a 5xx (overload) both keep it parked.
These tests pin that contract: target resolution per provider, the
2xx-vs-error decision, network-error → stay-parked, and the un-probeable
fallback to time-expiry optimism.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from roboco.runtime.orchestrator import (
    _HTTP_TOO_MANY_REQUESTS,
    AgentOrchestrator,
)

_HTTP_OK = 200


@pytest.fixture
def orch() -> AgentOrchestrator:
    return AgentOrchestrator.__new__(AgentOrchestrator)


def _fake_async_client(
    *, status_code: int | None = None, raise_exc: Exception | None = None
) -> MagicMock:
    """Patch target for ``httpx.AsyncClient`` — async ctx mgr whose get() responds."""
    response = MagicMock()
    response.status_code = status_code
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = (
        AsyncMock(side_effect=raise_exc)
        if raise_exc is not None
        else AsyncMock(return_value=response)
    )
    return MagicMock(return_value=client)


# ---------------------------------------------------------------------------
# _probe_target — provider → (url, headers)
# ---------------------------------------------------------------------------


def test_target_anthropic_is_never_probed() -> None:
    url, headers = AgentOrchestrator._probe_target("anthropic")
    assert url is None
    assert headers == {}


def test_target_ollama_uses_tags_endpoint() -> None:
    url, _headers = AgentOrchestrator._probe_target("ollama_cloud")
    assert url is not None
    assert url.endswith("/api/tags")


def test_target_unknown_provider_is_unprobeable() -> None:
    assert AgentOrchestrator._probe_target("mystery") == (None, {})


# ---------------------------------------------------------------------------
# _do_probe — the liveness decision
# ---------------------------------------------------------------------------


async def test_probe_ollama_ok_is_lifted(orch: AgentOrchestrator) -> None:
    fake = _fake_async_client(status_code=_HTTP_OK)
    with patch("roboco.runtime.orchestrator.httpx.AsyncClient", fake):
        assert await orch._do_probe("ollama_cloud") is True


async def test_probe_ollama_429_stays_limited(orch: AgentOrchestrator) -> None:
    fake = _fake_async_client(status_code=_HTTP_TOO_MANY_REQUESTS)
    with patch("roboco.runtime.orchestrator.httpx.AsyncClient", fake):
        assert await orch._do_probe("ollama_cloud") is False


@pytest.mark.parametrize("status", [500, 503, 529])
async def test_probe_ollama_5xx_stays_parked(
    orch: AgentOrchestrator, status: int
) -> None:
    """A 5xx (overload) keeps the provider parked — resuming would re-overload it."""
    fake = _fake_async_client(status_code=status)
    with patch("roboco.runtime.orchestrator.httpx.AsyncClient", fake):
        assert await orch._do_probe("ollama_cloud") is False


async def test_probe_network_error_stays_parked(orch: AgentOrchestrator) -> None:
    fake = _fake_async_client(raise_exc=httpx.ConnectError("boom"))
    with patch("roboco.runtime.orchestrator.httpx.AsyncClient", fake):
        assert await orch._do_probe("ollama_cloud") is False


async def test_probe_unprobeable_falls_back_to_optimism(
    orch: AgentOrchestrator,
) -> None:
    """Anthropic / unknown provider → trust the elapsed retry_after window, no HTTP."""
    with patch("roboco.runtime.orchestrator.httpx.AsyncClient") as client_cls:
        assert await orch._do_probe("anthropic") is True
        client_cls.assert_not_called()
