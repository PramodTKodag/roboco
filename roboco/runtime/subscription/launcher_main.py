"""Launcher process entry point: trusted configuration, startup reconcile, serve."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from roboco.runtime.subscription.launcher_policy import (
    ClientAuthenticator,
    LauncherConfig,
    LauncherRegistry,
)
from roboco.runtime.subscription.launcher_server import RUN_TTL, DockerCli, Launcher

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger("roboco.launcher")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_PINNED_PREFIXES = ("sha256:",)


class LauncherSettingsError(Exception):
    """Invalid launcher configuration. Messages never contain secret values."""


@dataclass(frozen=True)
class LauncherSettings:
    config: LauncherConfig
    auth: ClientAuthenticator
    host: str
    port: int


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise LauncherSettingsError(f"{name} is required")
    return value


def _read_token(env: Mapping[str, str], name: str) -> str:
    path = Path(_required(env, name))
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise LauncherSettingsError(f"{name} file is unreadable") from exc


def _is_pinned(image: str) -> bool:
    return "@sha256:" in image or image.startswith(_PINNED_PREFIXES)


def load_launcher_settings(env: Mapping[str, str]) -> LauncherSettings:
    image = _required(env, "ROBOCO_WORKER_IMAGE")
    if not _is_pinned(image):
        raise LauncherSettingsError("ROBOCO_WORKER_IMAGE must be a pinned digest")
    host = env.get("ROBOCO_LAUNCHER_HOST", DEFAULT_HOST)
    if host not in _LOOPBACK_HOSTS and (
        env.get("ROBOCO_LAUNCHER_ALLOW_NON_LOOPBACK") != "1"
    ):
        raise LauncherSettingsError(
            "non-loopback bind requires ROBOCO_LAUNCHER_ALLOW_NON_LOOPBACK=1"
        )
    try:
        config = LauncherConfig(
            image=image, runtime=env.get("ROBOCO_WORKER_RUNTIME", "runc")
        )
        auth = ClientAuthenticator(
            admin_token=_read_token(env, "ROBOCO_LAUNCHER_ADMIN_TOKEN_FILE"),
            executor_token=_read_token(env, "ROBOCO_LAUNCHER_EXECUTOR_TOKEN_FILE"),
        )
        port = int(env.get("ROBOCO_LAUNCHER_PORT", str(DEFAULT_PORT)))
    except ValueError as exc:
        raise LauncherSettingsError(str(exc)) from exc
    return LauncherSettings(config=config, auth=auth, host=host, port=port)


async def _serve(settings: LauncherSettings) -> None:
    launcher = Launcher(
        config=settings.config,
        registry=LauncherRegistry(run_ttl=timedelta(seconds=RUN_TTL.total_seconds())),
        auth=settings.auth,
        docker=DockerCli(),
    )
    await launcher.reconcile_orphans(dry_run=False)
    await launcher.serve(settings.host, settings.port)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        settings = load_launcher_settings(os.environ)
    except LauncherSettingsError as exc:
        logger.error("Launcher configuration refused: %s", exc)
        return 2
    asyncio.run(_serve(settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
