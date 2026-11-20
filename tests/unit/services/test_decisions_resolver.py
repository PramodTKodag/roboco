"""Unit tests for the Decisions tier resolver (spec section 1 chain)."""

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
import roboco.config as cfg
from roboco.services.decisions import resolver
from roboco.services.settings import SettingsService


@pytest.fixture(autouse=True)
def _reset_resolver_state() -> Iterator[None]:
    resolver.reset_health_cache()
    yield
    resolver.reset_health_cache()


def _flag_stack(
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool = True,
    clef: bool = True,
    laya: bool = True,
) -> None:
    monkeypatch.setattr(cfg.settings, "decisions_enabled", enabled)
    monkeypatch.setattr(cfg.settings, "decisions_tier_clef_enabled", clef)
    monkeypatch.setattr(cfg.settings, "decisions_tier_laya_enabled", laya)


@pytest.mark.asyncio
async def test_master_flag_off_resolves_to_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _flag_stack(monkeypatch, enabled=False)
    monkeypatch.setattr(resolver, "_backend_healthy", AsyncMock(return_value=True))
    assert await resolver.resolve_endpoint() is None


@pytest.mark.asyncio
async def test_healthy_clef_backend_resolves_to_clef(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _flag_stack(monkeypatch)
    monkeypatch.setattr(resolver, "_backend_healthy", AsyncMock(return_value=True))
    endpoint = await resolver.resolve_endpoint()
    assert endpoint is not None
    assert endpoint.tier == "clef"
    assert endpoint.base_url == cfg.settings.decisions_base_url
    assert endpoint.model == cfg.settings.decisions_model
    assert endpoint.timeout_s == cfg.settings.decisions_clef_timeout_s


@pytest.mark.asyncio
async def test_clef_backend_unhealthy_falls_to_cheap_laya(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clef down but Laya up: the cheap self-hosted tier serves instead of
    going without a verdict."""
    _flag_stack(monkeypatch)
    monkeypatch.setattr(
        resolver,
        "_backend_healthy",
        AsyncMock(side_effect=lambda tier: tier == "laya"),
    )
    endpoint = await resolver.resolve_endpoint()
    assert endpoint is not None
    assert endpoint.tier == "laya"
    assert endpoint.model == cfg.settings.decisions_laya_model
    assert endpoint.timeout_s == cfg.settings.decisions_timeout_s


@pytest.mark.asyncio
async def test_unhealthy_sidecar_resolves_to_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _flag_stack(monkeypatch, clef=False, laya=False)
    assert await resolver.resolve_endpoint() is None


@pytest.mark.asyncio
async def test_clef_disabled_laya_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clef tier disabled by config: the cheap Laya tier is the default
    server without any health drama."""
    _flag_stack(monkeypatch, clef=False, laya=True)
    monkeypatch.setattr(resolver, "_backend_healthy", AsyncMock(return_value=True))
    endpoint = await resolver.resolve_endpoint()
    assert endpoint is not None and endpoint.tier == "laya"


@pytest.mark.asyncio
async def test_health_probe_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    _flag_stack(monkeypatch)
    probe = AsyncMock(return_value=True)
    monkeypatch.setattr(resolver, "_backend_healthy", probe)
    await resolver.resolve_endpoint()
    await resolver.resolve_endpoint()
    assert probe.await_count == 2  # resolve is per-call, health is cached inside


class TestHealthBodyParsing:
    @staticmethod
    def _patch_probe(monkeypatch: pytest.MonkeyPatch, body: dict | None) -> None:
        async def _probe(base: str) -> dict | None:
            return body

        monkeypatch.setattr(resolver, "_probe_health_body", _probe)

    @pytest.mark.asyncio
    async def test_per_backend_body_routes_each_tier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_probe(
            monkeypatch,
            {"status": "degraded", "clef": {"ok": False}, "laya": {"ok": True}},
        )
        assert await resolver._backend_healthy("clef") is False
        assert await resolver._backend_healthy("laya") is True

    @pytest.mark.asyncio
    async def test_healthy_body_marks_both_tiers_ok(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_probe(
            monkeypatch,
            {
                "status": "ok",
                "clef": {"ok": True},
                "laya": {"ok": True},
            },
        )
        assert await resolver._backend_healthy("clef") is True
        assert await resolver._backend_healthy("laya") is True

    @pytest.mark.asyncio
    async def test_legacy_body_without_tier_detail_counts_healthy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pre-Clef sidecar body ({"status": "ok"}) keeps the old
        contract: sidecar up = serve."""
        self._patch_probe(monkeypatch, {"status": "ok"})
        assert await resolver._backend_healthy("clef") is True

    @pytest.mark.asyncio
    async def test_no_body_is_unhealthy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_probe(monkeypatch, None)
        assert await resolver._backend_healthy("clef") is False
        assert await resolver._backend_healthy("laya") is False

    @pytest.mark.asyncio
    async def test_body_cache_feeds_both_tiers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One /health fetch serves both tiers' probes in the same window."""
        _flag_stack(monkeypatch)
        fetches = 0

        class _FakeClient:
            def __init__(self, *a: object, **k: object) -> None:
                pass

            async def __aenter__(self) -> "_FakeClient":
                return self

            async def __aexit__(self, *a: object) -> None:
                return None

            async def get(self, url: str) -> MagicMock:
                nonlocal fetches
                fetches += 1
                response = MagicMock()
                response.status_code = 200
                response.json.return_value = {
                    "clef": {"ok": True},
                    "laya": {"ok": True},
                }
                return response

        monkeypatch.setattr(resolver.httpx, "AsyncClient", _FakeClient)
        assert await resolver._backend_healthy("clef") is True
        assert await resolver._backend_healthy("laya") is True
        assert fetches == 1


@pytest.mark.asyncio
async def test_pilot_mode_off_when_master_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    from roboco.services.decisions.pilots import PilotMode, pilot_mode

    monkeypatch.setattr(cfg.settings, "decisions_enabled", False)
    monkeypatch.setattr(SettingsService, "get", AsyncMock(return_value="on"))
    assert await pilot_mode(MagicMock(), "self_heal") is PilotMode.OFF


@pytest.mark.asyncio
async def test_pilot_mode_defaults_off_for_unset_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    from roboco.services.decisions.pilots import PilotMode, pilot_mode

    monkeypatch.setattr(cfg.settings, "decisions_enabled", True)
    monkeypatch.setattr(SettingsService, "get", AsyncMock(return_value=None))
    assert await pilot_mode(MagicMock(), "parking") is PilotMode.OFF


@pytest.mark.asyncio
async def test_pilot_mode_reads_shadow_row(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    from roboco.services.decisions.pilots import PilotMode, pilot_mode

    monkeypatch.setattr(cfg.settings, "decisions_enabled", True)
    monkeypatch.setattr(SettingsService, "get", AsyncMock(return_value="shadow"))
    assert await pilot_mode(MagicMock(), "steer_gate") is PilotMode.SHADOW


@pytest.mark.asyncio
async def test_pilot_mode_reads_tool_spotlight_row_as_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    from roboco.services.decisions.pilots import PilotMode, pilot_mode
    from roboco.services.settings import validate_setting

    validate_setting("decisions.pilot.tool_spotlight", "on")
    monkeypatch.setattr(cfg.settings, "decisions_enabled", True)
    monkeypatch.setattr(SettingsService, "get", AsyncMock(return_value="on"))
    assert await pilot_mode(MagicMock(), "tool_spotlight") is PilotMode.ON


@pytest.mark.asyncio
async def test_pilot_mode_env_on_list_arms_when_row_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator-deploy arming: an env slug list arms a pilot ON when the
    settings store has no row for it (NAS deploy posture)."""
    from unittest.mock import MagicMock

    from roboco.services.decisions.pilots import PilotMode, pilot_mode

    monkeypatch.setattr(cfg.settings, "decisions_enabled", True)
    monkeypatch.setattr(cfg.settings, "decisions_pilots_on", "self_heal,parking")
    monkeypatch.setattr(cfg.settings, "decisions_pilots_shadow", "")
    monkeypatch.setattr(SettingsService, "get", AsyncMock(return_value=None))
    assert await pilot_mode(MagicMock(), "self_heal") is PilotMode.ON
    assert await pilot_mode(MagicMock(), "parking") is PilotMode.ON


@pytest.mark.asyncio
async def test_pilot_mode_env_shadow_list_and_unset_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    from roboco.services.decisions.pilots import PilotMode, pilot_mode

    monkeypatch.setattr(cfg.settings, "decisions_enabled", True)
    monkeypatch.setattr(cfg.settings, "decisions_pilots_on", "self_heal")
    monkeypatch.setattr(cfg.settings, "decisions_pilots_shadow", "steer_gate")
    monkeypatch.setattr(SettingsService, "get", AsyncMock(return_value=None))
    assert await pilot_mode(MagicMock(), "steer_gate") is PilotMode.SHADOW
    assert await pilot_mode(MagicMock(), "never_heard_of_it") is PilotMode.OFF


@pytest.mark.asyncio
async def test_pilot_mode_settings_row_beats_env_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The panel row is the finer control: it wins over the env arming."""
    from unittest.mock import MagicMock

    from roboco.services.decisions.pilots import PilotMode, pilot_mode

    monkeypatch.setattr(cfg.settings, "decisions_enabled", True)
    monkeypatch.setattr(cfg.settings, "decisions_pilots_on", "self_heal")
    monkeypatch.setattr(SettingsService, "get", AsyncMock(return_value="shadow"))
    assert await pilot_mode(MagicMock(), "self_heal") is PilotMode.SHADOW
