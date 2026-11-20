"""The Claude service image ships exactly what its entrypoints import, pinned."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parents[3]
_DOCKERFILE = _REPO_ROOT / "docker" / "claude-subscription.Dockerfile"
_EXPORT_SCRIPT = _REPO_ROOT / "scripts" / "personal" / "export_worker_requirements.py"

_spec = importlib.util.spec_from_file_location(
    "export_worker_requirements_service", _EXPORT_SCRIPT
)
assert _spec is not None and _spec.loader is not None
export_script = importlib.util.module_from_spec(_spec)
sys.modules["export_worker_requirements_service"] = export_script
_spec.loader.exec_module(export_script)

_ENTRYPOINTS = (
    "roboco.runtime.subscription.service_main",
    "roboco.runtime.subscription.worker_tools_mcp",
)
_FORBIDDEN_PREFIXES = (
    "roboco.api",
    "roboco.services",
    "roboco.db",
    "roboco.llm",
    "roboco.runtime.orchestrator",
)


def _closure_probe(entrypoint: str) -> str:
    return (
        f"import sys, {entrypoint};"
        "print('\\n'.join(sorted(m for m in sys.modules if m.startswith('roboco'))))"
    )


def _imported_roboco_modules(entrypoint: str) -> list[str]:
    return subprocess.run(
        [sys.executable, "-P", "-c", _closure_probe(entrypoint)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()


def _copied_sources() -> list[str]:
    lines = re.findall(
        r"^COPY (roboco/\S+(?: roboco/\S+)*) /opt/service/",
        _DOCKERFILE.read_text(),
        re.M,
    )
    return [part for line in lines for part in line.split()]


def _module_is_copied(module: str, sources: list[str]) -> bool:
    relative = module.replace(".", "/")
    return any(
        source in (f"{relative}.py", relative, f"{relative}/__init__.py")
        or relative.startswith(source.removesuffix("/") + "/")
        for source in sources
    )


@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS)
def test_every_roboco_module_an_entrypoint_imports_is_copied(entrypoint: str) -> None:
    sources = _copied_sources()
    missing = [
        m
        for m in _imported_roboco_modules(entrypoint)
        if not _module_is_copied(m, sources)
    ]
    assert missing == []


@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS)
def test_entrypoints_never_import_application_code(entrypoint: str) -> None:
    imported = _imported_roboco_modules(entrypoint)
    assert [m for m in imported if m.startswith(_FORBIDDEN_PREFIXES)] == []


def test_service_requirements_are_hash_pinned_and_match_the_lockfile() -> None:
    text = export_script.SERVICE_OUTPUT.read_text()
    requirement_lines = [
        line for line in text.splitlines() if re.match(r"^[A-Za-z0-9_.-]+==", line)
    ]
    assert requirement_lines
    assert text.count("--hash=sha256:") >= len(requirement_lines)
    assert export_script.main(["--check"]) == 0


def test_service_requirements_are_a_superset_of_the_worker_requirements() -> None:
    assert set(export_script.WORKER_PACKAGES) < set(export_script.SERVICE_PACKAGES)


def test_dockerfile_is_pinned_verifies_the_cli_and_runs_unprivileged() -> None:
    dockerfile = _DOCKERFILE.read_text()
    assert re.search(r"^FROM \S+@sha256:[0-9a-f]{64}$", dockerfile, re.M)
    assert "pip install --require-hashes --no-deps -r" in dockerfile
    assert "sha256sum --check --strict" in dockerfile
    assert re.search(r"^USER 10002:10002$", dockerfile, re.M)
    assert "docker.sock" not in dockerfile
    assert "apt-get" not in dockerfile
    assert "git" not in re.findall(r"^RUN .*", dockerfile, re.M)
