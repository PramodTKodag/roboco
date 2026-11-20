"""Fixtures for the real-Docker subscription suites."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
from roboco.runtime.subscription.launcher_policy import VOLUME_PREFIX

from tests.integration.subscription.docker_support import (
    IMAGE_TAG,
    REPO_ROOT,
    docker,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_REMOVE_ATTEMPTS = 10


@pytest.fixture(scope="session")
def worker_image() -> str:
    docker(
        "build",
        "--quiet",
        "--file",
        str(REPO_ROOT / "docker" / "task-worker.Dockerfile"),
        "--tag",
        IMAGE_TAG,
        str(REPO_ROOT),
    )
    return IMAGE_TAG


@pytest.fixture
def created_volumes() -> Iterator[list[str]]:
    volumes: list[str] = []
    yield volumes
    for name in volumes:
        assert name.startswith(VOLUME_PREFIX)
        # An auto-removed worker container can still be releasing the volume.
        for _ in range(_REMOVE_ATTEMPTS):
            if docker("volume", "rm", "--force", name, check=False).returncode == 0:
                break
            time.sleep(0.5)
