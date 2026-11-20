"""The built deployment images carry no credential, owner state or repository history.

Container mode: every `roboco-personal-*` image built from this tree is exported
and its file list and application files are scanned. Nothing is run and no
credential is read; images that are not built are skipped.
"""

from __future__ import annotations

import re
import subprocess
import tarfile
from typing import TYPE_CHECKING, Final

import pytest
from roboco.runtime.subscription.cli_pin import (
    PINNED_BINARY_SHA256,
    PINNED_CLI_VERSION,
    sha256_of_file,
)

from tests.integration.subscription.docker_support import (
    REPO_ROOT,
    docker,
    docker_unavailable_reason,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_APPROVED_BUILD_MISMATCH_EXIT_CODE: Final = 2
_SERVICES = ("controller", "launcher", "claude-service", "panel", "worker")
_FORBIDDEN_FILE_NAMES = frozenset(
    {
        ".credentials.json",
        ".claude.json",
        ".netrc",
        ".npmrc",
        ".git-credentials",
        "id_rsa",
        "id_ed25519",
        "secrets.env",
        "derived.env",
        "panel-login.txt",
    }
)
_FORBIDDEN_DIRECTORIES = (".git", ".ssh", ".aws", ".personal", ".probe-cache", ".env")
_APPLICATION_ROOTS = (
    "app/roboco",
    "app/agents",
    "app/docs",
    "app/alembic",
    "app/public",
    "app/.next",
    "opt/launcher",
    "opt/service",
    "opt/worker",
    "etc/roboco",
    "home/",
)
_SECRET_SHAPES = re.compile(
    rb"sk-ant-[A-Za-z0-9_-]{20,}"
    rb"|ANTHROPIC_API_KEY\s*=\s*\S+"
    rb"|CLAUDE_CODE_OAUTH_TOKEN\s*=\s*\S+"
    rb"|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s*[A-Za-z0-9+/=\s]{100,}"
)
_MAX_SCANNED_BYTES = 2_000_000


def _latest_image(service: str) -> str | None:
    listing = docker(
        "images",
        "--format",
        "{{.Repository}}:{{.Tag}}",
        f"roboco-personal-{service}",
        check=False,
    ).stdout.split()
    built = [name for name in listing if not name.endswith((":itest", ":dev"))]
    return built[0] if built else None


def _unavailable() -> str | None:
    if (reason := docker_unavailable_reason()) is not None:
        return reason
    if not any(_latest_image(service) for service in _SERVICES):
        return "no personal deployment image is built (run `make personal-up`)"
    return None


_UNAVAILABLE = _unavailable()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(_UNAVAILABLE is not None, reason=_UNAVAILABLE or "available"),
]


def _export(image: str, destination: Path) -> Path:
    container = docker("create", image, check=True).stdout.strip()
    archive = destination / "image.tar"
    try:
        with archive.open("wb") as handle:
            subprocess.run(
                ["docker", "export", container],
                stdout=handle,
                check=True,
                timeout=300,
            )
    finally:
        docker("rm", "-f", container, check=False)
    return archive


def _members(archive: Path) -> Iterator[tarfile.TarInfo]:
    with tarfile.open(archive) as tar:
        yield from tar


@pytest.mark.parametrize("service", _SERVICES)
def test_image_has_no_credential_file_state_or_history(
    service: str, tmp_path: Path
) -> None:
    image = _latest_image(service)
    if image is None:
        pytest.skip(f"{service} image is not built")
    archive = _export(image, tmp_path)
    offenders: list[str] = []
    for member in _members(archive):
        parts = member.name.strip("./").split("/")
        in_package = "site-packages" in parts or "node_modules" in parts
        if parts[-1] in _FORBIDDEN_FILE_NAMES and not in_package:
            offenders.append(member.name)
        if not in_package and any(part in _FORBIDDEN_DIRECTORIES for part in parts):
            offenders.append(member.name)
    assert offenders == [], (image, offenders[:10])


@pytest.mark.parametrize("service", _SERVICES)
def test_application_files_contain_no_credential_shapes(
    service: str, tmp_path: Path
) -> None:
    image = _latest_image(service)
    if image is None:
        pytest.skip(f"{service} image is not built")
    archive = _export(image, tmp_path)
    offenders: list[str] = []
    scanned = 0
    with tarfile.open(archive) as tar:
        for member in tar:
            name = member.name.strip("./")
            if not member.isfile() or member.size > _MAX_SCANNED_BYTES:
                continue
            if not name.startswith(_APPLICATION_ROOTS) or "/.venv/" in f"/{name}":
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            scanned += 1
            if _SECRET_SHAPES.search(handle.read()):
                offenders.append(name)
    assert scanned > 0, f"{image}: the scan covered no application files"
    assert offenders == [], (image, offenders[:10])


def test_service_images_do_not_run_as_root_except_the_socket_holder() -> None:
    expected_root = {"launcher"}
    for service in _SERVICES:
        image = _latest_image(service)
        if image is None:
            continue
        user = docker(
            "image", "inspect", "--format", "{{.Config.User}}", image
        ).stdout.strip()
        is_root = user in {"", "0", "root", "0:0"}
        assert is_root == (service in expected_root), (service, user)


_SERVICE_IMAGE_SOURCES = (
    "roboco/runtime/subscription/cli.py",
    "roboco/runtime/subscription/cli_pin.py",
    "roboco/runtime/subscription/policy.py",
    "roboco/runtime/subscription/service_main.py",
)


def _in_image(image: str, *command: str, volume: str | None = None) -> str:
    mount = ["-v", volume] if volume else []
    return docker(
        "run", "--rm", "--network", "none", *mount, "--entrypoint", command[0], image,
        *command[1:],
        check=False,
    ).stdout  # fmt: skip


def test_the_shipped_cli_is_the_approved_build() -> None:
    image = _latest_image("claude-service")
    if image is None:
        pytest.skip("claude-service image is not built")
    digest = _in_image(image, "sha256sum", "/opt/claude/claude").split()[0]
    assert digest in PINNED_BINARY_SHA256.values()
    version = _in_image(image, "/opt/claude/claude", "--version")
    assert PINNED_CLI_VERSION in version


def test_the_shipped_trusted_policy_files_match_this_tree() -> None:
    image = _latest_image("claude-service")
    if image is None:
        pytest.skip("claude-service image is not built")
    for relative in _SERVICE_IMAGE_SOURCES:
        shipped = _in_image(image, "sha256sum", f"/opt/service/{relative}").split()[0]
        assert shipped == sha256_of_file(REPO_ROOT / relative), relative


def test_unapproved_cli_contract_blocks(tmp_path: Path) -> None:
    """A changed binary in the real image stops the service before it serves."""
    image = _latest_image("claude-service")
    if image is None:
        pytest.skip("claude-service image is not built")
    substitute = tmp_path / "claude"
    substitute.write_bytes(b"#!/bin/sh\nexit 0\n")
    substitute.chmod(0o755)
    result = docker(
        "run", "--rm", "--network", "none",
        "-v", f"{substitute}:/opt/claude/claude:ro",
        image,
        check=False,
    )  # fmt: skip
    assert result.returncode == _APPROVED_BUILD_MISMATCH_EXIT_CODE
    assert "does not match approved build" in result.stderr + result.stdout
