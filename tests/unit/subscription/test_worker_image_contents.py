"""The worker image ships exactly what the entrypoint imports, with pinned deps."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parents[3]
_DOCKERFILE = _REPO_ROOT / "docker" / "task-worker.Dockerfile"
_EXPORT_SCRIPT = _REPO_ROOT / "scripts" / "personal" / "export_worker_requirements.py"

_spec = importlib.util.spec_from_file_location(
    "export_worker_requirements", _EXPORT_SCRIPT
)
assert _spec is not None and _spec.loader is not None
export_script = importlib.util.module_from_spec(_spec)
sys.modules["export_worker_requirements"] = export_script
_spec.loader.exec_module(export_script)

_CLOSURE_PROBE = (
    "import sys, roboco.runtime.subscription.worker_entrypoint;"
    "print('\\n'.join(sorted(m for m in sys.modules if m.split('.')[0]"
    " in ('roboco',) or '.' not in m)))"
)


def _copied_roboco_sources() -> list[str]:
    return re.findall(
        r"^COPY (roboco/\S+(?: roboco/\S+)*) /opt/worker/",
        _DOCKERFILE.read_text(),
        re.M,
    )


def _imported_roboco_modules() -> list[str]:
    output = subprocess.run(
        [sys.executable, "-P", "-c", _CLOSURE_PROBE],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [m for m in output if m.startswith("roboco")]


def _module_is_copied(module: str, copied: list[str]) -> bool:
    relative = module.replace(".", "/")
    sources = [part for line in copied for part in line.split()]
    return any(
        source in (f"{relative}.py", relative)
        or source == f"{relative}/__init__.py"
        or relative.startswith(source.removesuffix("/") + "/")
        or relative == source.removesuffix(".py")
        for source in sources
    )


def test_every_roboco_module_the_worker_imports_is_copied_into_the_image() -> None:
    copied = _copied_roboco_sources()
    missing = [
        m for m in _imported_roboco_modules() if not _module_is_copied(m, copied)
    ]
    assert missing == []


def test_worker_never_imports_provider_or_application_code() -> None:
    forbidden = (
        "roboco.api",
        "roboco.services",
        "roboco.db",
        "roboco.llm",
        "roboco.runtime.orchestrator",
    )
    imported = _imported_roboco_modules()
    assert [m for m in imported if m.startswith(forbidden)] == []


def test_requirements_are_hash_pinned_and_match_the_lockfile() -> None:
    text = export_script.OUTPUT.read_text()
    requirement_lines = [
        line for line in text.splitlines() if re.match(r"^[A-Za-z0-9_.-]+==", line)
    ]
    assert requirement_lines
    assert text.count("--hash=sha256:") >= len(requirement_lines)
    assert export_script.main(["--check"]) == 0


def test_dockerfile_is_pinned_and_installs_only_hashed_requirements() -> None:
    dockerfile = _DOCKERFILE.read_text()
    assert re.search(r"^FROM \S+@sha256:[0-9a-f]{64}$", dockerfile, re.M)
    assert "pip install --require-hashes --no-deps -r" in dockerfile
    assert re.search(r"^USER 10001:10001$", dockerfile, re.M)
    assert "docker.sock" not in dockerfile
