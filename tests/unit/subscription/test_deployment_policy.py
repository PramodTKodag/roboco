"""Static policy for the personal deployment (compose.personal.yml and its images).

These checks read the files only; the live stack is exercised by the container
suites. Each one pins a boundary the deployment must never lose.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from roboco.foundation.policy.subscription_policy import (
    FORBIDDEN_ENV_NAMES,
    FORBIDDEN_ENV_PREFIXES,
)

_REPO_ROOT = Path(__file__).parents[3]
_COMPOSE = _REPO_ROOT / "compose.personal.yml"
_DOCKER_DIR = _REPO_ROOT / "docker"

_LAUNCHER = "launcher"
_CONTROLLERS = ("orchestrator", "dispatcher")
_REFUSED_EXIT_CODE = 2
_DOCKER_SOCKET = "/var/run/docker.sock"
_EGRESS_POLICY_MODULE = "./roboco/runtime/subscription/egress_policy.py"
_LOCALLY_BUILT_IMAGE = re.compile(r"^roboco-personal-[a-z-]+:\$\{ROBOCO_PERSONAL_TAG")
_DIGEST_PINNED_IMAGE = re.compile(r"@sha256:[0-9a-f]{64}$")
_HOST_HOME_MARKERS = ("~", "$HOME", "${HOME}", ".claude", ".config", "Keychain")
_FORBIDDEN_SERVICE_WORDS = (
    "ollama",
    "model-pull",
    "openrouter",
    "hummin",
    "nebius",
    "codex",
    "gemini",
    "grok",
    "kimi",
)


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def services(compose: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return compose["services"]  # type: ignore[no-any-return]


def _environment(service: dict[str, Any]) -> dict[str, str]:
    raw = service.get("environment") or {}
    if isinstance(raw, list):
        return dict(item.split("=", 1) for item in raw)
    return {str(key): str(value) for key, value in raw.items()}


def _is_forbidden_name(name: str) -> bool:
    return name in FORBIDDEN_ENV_NAMES or name.startswith(FORBIDDEN_ENV_PREFIXES)


def test_every_image_is_pinned_or_built_from_this_tree(
    services: dict[str, dict[str, Any]],
) -> None:
    for name, service in services.items():
        image = service["image"]
        built_here = "build" in service and _LOCALLY_BUILT_IMAGE.match(image)
        assert built_here or _DIGEST_PINNED_IMAGE.search(image), (name, image)
        assert not image.endswith(":latest"), name


_DEPLOYMENT_DOCKERFILES = (
    "controller.Dockerfile",
    "launcher.Dockerfile",
    "panel.Dockerfile",
    "claude-subscription.Dockerfile",
    "task-worker.Dockerfile",
)


@pytest.mark.parametrize("dockerfile", _DEPLOYMENT_DOCKERFILES)
def test_dockerfile_base_images_are_digest_pinned(dockerfile: str) -> None:
    stages: set[str] = set()
    for line in (_DOCKER_DIR / dockerfile).read_text(encoding="utf-8").splitlines():
        words = line.split()
        if not words or words[0] != "FROM":
            continue
        reference = next(word for word in words[1:] if not word.startswith("--"))
        if reference not in stages:
            assert _DIGEST_PINNED_IMAGE.search(reference), (dockerfile, line)
        if "AS" in words:
            stages.add(words[words.index("AS") + 1])


def test_only_the_launcher_holds_the_docker_socket(
    services: dict[str, dict[str, Any]],
) -> None:
    holders = {
        name
        for name, service in services.items()
        if any(_DOCKER_SOCKET in str(volume) for volume in service.get("volumes", []))
    }
    assert holders == {_LAUNCHER}


def test_only_the_edge_publishes_a_port_and_only_on_loopback(
    services: dict[str, dict[str, Any]],
) -> None:
    published = {
        name: service["ports"]
        for name, service in services.items()
        if "ports" in service
    }
    assert set(published) == {"nginx"}
    assert all(str(port).startswith("127.0.0.1:") for port in published["nginx"])


def test_no_service_environment_or_build_arg_carries_a_credential_name(
    compose: dict[str, Any], services: dict[str, dict[str, Any]]
) -> None:
    for name, service in services.items():
        for variable in _environment(service):
            assert not _is_forbidden_name(variable), (name, variable)
        build = service.get("build")
        if isinstance(build, dict):
            for arg in build.get("args", {}) or {}:
                assert not _is_forbidden_name(arg), (name, arg)
    for anchor in ("x-controller-env", "x-hardened"):
        for variable in compose[anchor] if isinstance(compose[anchor], dict) else ():
            assert not _is_forbidden_name(variable), (anchor, variable)


def test_no_host_credential_or_project_home_is_mounted(
    services: dict[str, dict[str, Any]],
) -> None:
    for name, service in services.items():
        for volume in service.get("volumes", []):
            source = str(volume).split(":", 1)[0]
            is_bind = source.startswith(("/", ".", "~", "$"))
            if not is_bind:
                continue
            assert not any(marker in source for marker in _HOST_HOME_MARKERS), (
                name,
                volume,
            )
            allowed = source in (
                _DOCKER_SOCKET,
                _EGRESS_POLICY_MODULE,
            ) or source.startswith("./deploy/personal/")
            assert allowed, (name, volume)
            assert source == _DOCKER_SOCKET or str(volume).endswith(":ro"), (
                name,
                volume,
            )


def test_service_names_do_not_include_other_providers_or_model_pulls(
    services: dict[str, dict[str, Any]],
) -> None:
    for name in services:
        assert not any(word in name for word in _FORBIDDEN_SERVICE_WORDS), name


def test_every_runtime_service_drops_all_capabilities_and_blocks_escalation(
    services: dict[str, dict[str, Any]],
) -> None:
    for name, service in services.items():
        if "profiles" in service:
            continue
        assert service.get("cap_drop") == ["ALL"], name
        assert "no-new-privileges:true" in service.get("security_opt", []), name
        assert not service.get("privileged"), name
        assert service.get("network_mode", "none") == "none", name


def test_internal_networks_are_internal_and_only_the_proxy_reaches_the_internet(
    compose: dict[str, Any], services: dict[str, dict[str, Any]]
) -> None:
    networks = compose["networks"]
    assert networks["runtime"]["internal"] is True
    assert networks["data"]["internal"] is True
    on_egress = {
        name
        for name, service in services.items()
        if "egress" in (service.get("networks") or {})
    }
    assert on_egress == {"egress-proxy"}
    on_edge = {
        name
        for name, service in services.items()
        if "edge" in (service.get("networks") or {})
    }
    assert on_edge == {"nginx"}


def test_database_and_cache_are_reachable_only_from_the_controllers(
    services: dict[str, dict[str, Any]],
) -> None:
    on_data = {
        name
        for name, service in services.items()
        if "data" in (service.get("networks") or {})
    }
    assert on_data == {"postgres", "redis", *_CONTROLLERS}


def test_the_credential_volume_is_mounted_only_in_the_claude_service(
    services: dict[str, dict[str, Any]],
) -> None:
    holders = {
        name
        for name, service in services.items()
        if any("claude_credentials" in str(v) for v in service.get("volumes", []))
    }
    assert holders == {"claude-service"}


def test_the_launcher_admin_token_reaches_only_the_controllers_and_the_launcher(
    services: dict[str, dict[str, Any]],
) -> None:
    holders = {
        name
        for name, service in services.items()
        if "launcher_admin_token" in (service.get("secrets") or [])
    }
    assert holders == {*_CONTROLLERS, _LAUNCHER}


def test_the_claude_service_has_no_database_launcher_admin_or_docker_access(
    services: dict[str, dict[str, Any]],
) -> None:
    service = services["claude-service"]
    assert service["networks"] == ["runtime"]
    assert "launcher_admin_token" not in service["secrets"]
    environment = _environment(service)
    assert not any("DATABASE" in key or "REDIS" in key for key in environment)


def test_egress_defaults_to_the_normal_profile(
    services: dict[str, dict[str, Any]],
) -> None:
    profile = _environment(services["egress-proxy"])["ROBOCO_EGRESS_PROFILE"]
    assert profile == "${ROBOCO_EGRESS_PROFILE:-normal}"


def test_the_controller_runs_without_a_container_runtime(
    services: dict[str, dict[str, Any]],
) -> None:
    for name in _CONTROLLERS:
        environment = _environment(services[name])
        assert environment["ROBOCO_CONTAINER_RUNTIME_ENABLED"] == "false", name
        assert services[name].get("read_only") is True, name


def test_secrets_are_files_and_never_inline(
    compose: dict[str, Any], services: dict[str, dict[str, Any]]
) -> None:
    for name, secret in compose["secrets"].items():
        assert "file" in secret and "environment" not in secret, name
    for name, service in services.items():
        for key, value in _environment(service).items():
            if key.endswith(("PASSWORD", "SECRET", "ENCRYPTION_KEY")):
                assert value.startswith("${"), (name, key)


@pytest.mark.parametrize(
    "dockerfile",
    sorted(path.name for path in _DOCKER_DIR.glob("*.Dockerfile.dockerignore")),
)
def test_build_context_allowlist_matches_the_copy_sources(dockerfile: str) -> None:
    ignore_file = _DOCKER_DIR / dockerfile
    rules = [
        line.strip()
        for line in ignore_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert rules[0] == "*"
    allowed = {rule[1:] for rule in rules if rule.startswith("!")}
    copy_sources: set[str] = set()
    source = _DOCKER_DIR / dockerfile.removesuffix(".dockerignore")
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.startswith("COPY ") or "--from=" in line:
            continue
        parts = [p for p in line.split()[1:] if not p.startswith("--")]
        copy_sources.update(part.rstrip("/") for part in parts[:-1])
    for needed in copy_sources:
        covered = any(
            needed == rule or needed.startswith(rule + "/") for rule in allowed
        )
        assert covered, (dockerfile, needed)
    for rule in allowed:
        assert (_REPO_ROOT / rule).exists(), (dockerfile, rule)


def test_hosts_origins_and_edge_port_share_one_setting(
    compose: dict[str, Any], services: dict[str, dict[str, Any]]
) -> None:
    port = "${ROBOCO_PERSONAL_PORT:-3300}"
    environment = compose["x-controller-env"]
    for name in ("ROBOCO_ALLOWED_HOSTS", "ROBOCO_CORS_ORIGINS"):
        assert f"localhost:{port}" in environment[name], name
        assert f"127.0.0.1:{port}" in environment[name], name
    assert services["nginx"]["ports"] == [f"127.0.0.1:{port}:80"]
    assert _environment(services["nginx"])["ROBOCO_PERSONAL_PORT"] == port


_LEGACY_CONTROLLERS = {
    "docker-compose.yml": (
        "orchestrator-blue",
        "orchestrator-green",
        "dispatcher-blue",
        "dispatcher-green",
    ),
    "docker-compose.yaml": (
        "orchestrator-blue",
        "orchestrator-green",
        "dispatcher-blue",
        "dispatcher-green",
    ),
    "docker-compose.registry.yml": ("orchestrator", "dispatcher"),
}


@pytest.mark.parametrize(("filename", "names"), _LEGACY_CONTROLLERS.items())
def test_legacy_compose_controllers_refuse_to_start(
    filename: str, names: tuple[str, ...]
) -> None:
    legacy = yaml.safe_load((_REPO_ROOT / filename).read_text(encoding="utf-8"))
    for name in names:
        service = legacy["services"][name]
        assert service["restart"] == "no", (filename, name)
        script = service["entrypoint"][-1]
        assert "make personal-up" in script, (filename, name)
        assert script.rstrip().endswith("exit 2"), (filename, name)


def test_the_refusal_entrypoint_exits_non_zero_with_the_message() -> None:
    legacy = yaml.safe_load(
        (_REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )
    entrypoint = legacy["services"]["orchestrator-blue"]["entrypoint"]
    result = subprocess.run(  # nosec B603
        entrypoint, capture_output=True, text=True, check=False, timeout=10
    )
    assert result.returncode == _REFUSED_EXIT_CODE
    assert "make personal-up" in result.stderr
