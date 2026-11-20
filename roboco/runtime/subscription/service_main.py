"""Claude service entry point: trusted configuration, gateway socket, serve."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from roboco.runtime.subscription.cli import CLAUDE_BINARY
from roboco.runtime.subscription.cli_pin import CliBuildNotApproved, verify_cli_binary
from roboco.runtime.subscription.gateway_socket import GatewayServer
from roboco.runtime.subscription.launcher_client import WorkerLauncher
from roboco.runtime.subscription.policy import (
    EgressRoute,
    EgressRouteInvalid,
    parse_egress_route,
)
from roboco.runtime.subscription.service import ClaudeService
from roboco.runtime.subscription.service_server import ServiceFrontend
from roboco.runtime.subscription.tool_gateway import ToolGateway

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger("roboco.claude_service")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
DEFAULT_RUNTIME_DIR = Path("/run/roboco/runs")
DEFAULT_SOCKET_PATH = Path("/run/roboco/gateway/gateway.sock")


class ServiceSettingsError(Exception):
    """Invalid service configuration. Messages never contain secret values."""


@dataclass(frozen=True)
class ServiceSettings:
    host: str
    port: int
    service_token: str
    launcher_host: str
    launcher_port: int
    launcher_executor_token: str
    runtime_dir: Path
    socket_path: Path
    orchestrator_url: str
    egress: EgressRoute


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ServiceSettingsError(f"{name} is required")
    return value


def _read_token(env: Mapping[str, str], name: str) -> str:
    try:
        return Path(_required(env, name)).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ServiceSettingsError(f"{name} file is unreadable") from exc


def _orchestrator_url(env: Mapping[str, str]) -> str:
    """The orchestrator the verb servers call; empty means verbs are refused."""
    url = env.get("ROBOCO_ORCHESTRATOR_URL", "").strip()
    if url and urlsplit(url).scheme not in {"http", "https"}:
        raise ServiceSettingsError("ROBOCO_ORCHESTRATOR_URL must be an http(s) URL")
    return url.rstrip("/")


def _egress_route(env: Mapping[str, str]) -> EgressRoute:
    """The proxy and CA are mandatory: the service never runs without them."""
    try:
        route = parse_egress_route(
            _required(env, "ROBOCO_EGRESS_PROXY_URL"),
            _required(env, "ROBOCO_EGRESS_CA_FILE"),
        )
    except EgressRouteInvalid as exc:
        raise ServiceSettingsError(f"egress route refused: {exc}") from exc
    if not Path(route.ca_file).is_file():
        raise ServiceSettingsError("ROBOCO_EGRESS_CA_FILE is unreadable")
    return route


def load_service_settings(env: Mapping[str, str]) -> ServiceSettings:
    host = env.get("ROBOCO_CLAUDE_SERVICE_HOST", DEFAULT_HOST)
    if host not in _LOOPBACK_HOSTS and (
        env.get("ROBOCO_CLAUDE_SERVICE_ALLOW_NON_LOOPBACK") != "1"
    ):
        raise ServiceSettingsError(
            "non-loopback bind requires ROBOCO_CLAUDE_SERVICE_ALLOW_NON_LOOPBACK=1"
        )
    try:
        port = int(env.get("ROBOCO_CLAUDE_SERVICE_PORT", str(DEFAULT_PORT)))
        launcher_port = int(_required(env, "ROBOCO_LAUNCHER_PORT"))
    except ValueError as exc:
        raise ServiceSettingsError("a port setting is not an integer") from exc
    return ServiceSettings(
        host=host,
        port=port,
        service_token=_read_token(env, "ROBOCO_CLAUDE_SERVICE_TOKEN_FILE"),
        launcher_host=_required(env, "ROBOCO_LAUNCHER_HOST"),
        launcher_port=launcher_port,
        launcher_executor_token=_read_token(env, "ROBOCO_LAUNCHER_EXECUTOR_TOKEN_FILE"),
        runtime_dir=Path(
            env.get("ROBOCO_CLAUDE_RUNTIME_DIR", str(DEFAULT_RUNTIME_DIR))
        ),
        socket_path=Path(
            env.get("ROBOCO_GATEWAY_SOCKET_PATH", str(DEFAULT_SOCKET_PATH))
        ),
        orchestrator_url=_orchestrator_url(env),
        egress=_egress_route(env),
    )


async def _serve(settings: ServiceSettings) -> None:
    gateway = ToolGateway(
        WorkerLauncher(
            settings.launcher_host,
            settings.launcher_port,
            settings.launcher_executor_token,
        )
    )
    gateway_server = GatewayServer(gateway)
    await gateway_server.start(settings.socket_path)
    service = ClaudeService(
        gateway=gateway,
        runtime_dir=settings.runtime_dir,
        socket_path=settings.socket_path,
        orchestrator_url=settings.orchestrator_url,
        egress=settings.egress,
    )
    frontend = ServiceFrontend(service, token=settings.service_token)
    try:
        await frontend.serve(settings.host, settings.port)
    finally:
        await gateway_server.stop()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        verify_cli_binary(CLAUDE_BINARY)
    except CliBuildNotApproved as exc:
        logger.error("Claude service refused to start: %s", exc)
        return 2
    try:
        settings = load_service_settings(os.environ)
    except ServiceSettingsError as exc:
        logger.error("Claude service configuration refused: %s", exc)
        return 2
    asyncio.run(_serve(settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
