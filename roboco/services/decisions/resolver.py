"""Per-call tier resolution for the Decisions service.

The resolution chain (spec section 1), evaluated cheaply and in-process on
every call: master flag off -> no verdict (the pre-Decisions floor). Clef
tier enabled (default) and the sidecar's Clef backend healthy -> the
sweet-spot default. Clef disabled/unhealthy -> the cheap self-hosted Laya
backend IF enabled and healthy. Both backends unavailable -> the floor.

The selected tier is stamped into every ``session_id`` and every log line
so shadow data stays attributable per backend (Clef is the default
serving tier; Laya is the cheap baseline).
"""

from __future__ import annotations

import time

import httpx
import structlog

from roboco.config import settings
from roboco.services.decisions.client import DecisionsEndpoint

logger = structlog.get_logger(__name__)

# Cached sidecar per-backend health (spec 4): a GET /health probe with a 30s
# TTL instead of a probe per call. The probe returns one body covering BOTH
# self-hosted backends ({"clef": {...ok...}, "laya": {...ok...}}), so one
# fetch feeds two cache entries. An unhealthy backend resolves past its tier
# (unhealthy Clef -> the cheap Laya tier; both unhealthy -> the floor) for
# the cache window.
_HEALTH_TTL_SECONDS = 30.0
_health_cache: dict[str, tuple[float, bool]] = {}
# The raw /health body cache (one fetch feeds both tiers' probes). A
# separate dict: its values are parsed bodies, not booleans.
_health_body_cache: dict[str, tuple[float, dict | None]] = {}
_HEALTH_BODY_CACHE_KEY = "sidecar-health-body"


async def resolve_endpoint() -> DecisionsEndpoint | None:
    """Pick the tier that should serve this call, or ``None`` for no verdict
    (every caller then does exactly what it did before Decisions existed)."""
    if not settings.decisions_enabled:
        return None

    if settings.decisions_tier_clef_enabled and await _backend_healthy("clef"):
        return _clef_endpoint()

    if settings.decisions_tier_laya_enabled and await _backend_healthy("laya"):
        return _laya_endpoint()

    return None


def _clef_endpoint() -> DecisionsEndpoint:
    return DecisionsEndpoint(
        tier="clef",
        base_url=settings.decisions_base_url,
        model=settings.decisions_model,
        timeout_s=settings.decisions_clef_timeout_s,
    )


def _laya_endpoint() -> DecisionsEndpoint:
    return DecisionsEndpoint(
        tier="laya",
        base_url=settings.decisions_base_url,
        model=settings.decisions_laya_model,
        timeout_s=settings.decisions_timeout_s,
    )


async def _backend_healthy(tier: str) -> bool:
    """Cached per-backend GET /health probe (30s TTL, one HTTP fetch feeds
    both tiers' cache entries). Network trouble counts as unhealthy for the
    window; never raises. A legacy sidecar body without per-backend detail
    (pre-Clef image) counts as healthy for whichever tier is probed: the
    deploy swaps image and orchestrator together, and fail-open beats
    fail-stuck during the overlap."""
    base = settings.decisions_base_url.rstrip("/")
    now = time.monotonic()
    cached = _health_cache.get(tier)
    if cached is not None and now - cached[0] < _HEALTH_TTL_SECONDS:
        return cached[1]
    body = await _probe_health_body(base)
    ok = _tier_ok_from_body(body, tier)
    _health_cache[tier] = (now, ok)
    if not ok:
        logger.warning(
            "roboco-decisions backend unhealthy; decisions resolve past this tier",
            tier=tier,
            base_url=base,
        )
    return ok


async def _probe_health_body(base: str) -> dict | None:
    """One GET /health returning the parsed JSON body (or ``None``). The
    body is also cached so the second tier's probe in the same window never
    re-fetches."""
    now = time.monotonic()
    cached = _health_body_cache.get(_HEALTH_BODY_CACHE_KEY)
    if cached is not None and now - cached[0] < _HEALTH_TTL_SECONDS:
        return cached[1]
    body = None
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{base}/health")
            acceptable = (httpx.codes.OK, httpx.codes.SERVICE_UNAVAILABLE)
            if response.status_code in acceptable:
                parsed = response.json()
                if isinstance(parsed, dict):
                    body = parsed
    except (httpx.HTTPError, ValueError, OSError) as exc:
        logger.debug("roboco-decisions health probe failed", error=str(exc))
    _health_body_cache[_HEALTH_BODY_CACHE_KEY] = (now, body)
    return body


def _tier_ok_from_body(body: dict | None, tier: str) -> bool:
    """Read one tier's health out of a /health body. Missing tier detail
    (legacy body, backend disabled) counts as UNhealthy for a per-tier
    probe only when the body explicitly names the tier as not ok; a body
    with no per-tier shape at all is the legacy contract "sidecar is up =
    serve"."""
    if body is None:
        return False
    detail = body.get(tier)
    if detail is None:
        return not any(key in body for key in ("clef", "laya"))
    if isinstance(detail, dict):
        return bool(detail.get("ok", False))
    return bool(detail)


def reset_health_cache() -> None:
    """Test hook: drop the cached probes and the cached body."""
    _health_cache.clear()
    _health_body_cache.clear()
