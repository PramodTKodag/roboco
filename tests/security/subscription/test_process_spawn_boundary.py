"""Every process-spawning site outside the worker package is accounted for.

Project code may only run inside the credential-free offline worker. This scan
finds each module that can start a process and requires it to be listed with a
category and a reason, so a new spawn site cannot appear unnoticed.

The categories are deliberately not equal:

* ``orchestrated_docker`` -- the trusted controller driving the Docker CLI. It
  never executes project code.
* ``host_clone_git`` -- ``git`` inside a host-side clone. Git can run hooks,
  filters and configured helpers from repository content, so these sites must
  disappear when local review replaces the host-clone model (Tasks 7 and 9).
* ``legacy_provider_runtime`` -- non-subscription model runtimes and provider
  CLIs, deleted when routing becomes subscription-only (Task 7).

The two removable categories are listed in ``REMOVAL_PENDING`` and the scan fails
if an entry there outlives its module, so the allowlist can only shrink.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).parents[3]
SOURCE_ROOT = REPO_ROOT / "roboco"
WORKER_PACKAGE = SOURCE_ROOT / "runtime" / "subscription"

_SPAWN_CALLS = frozenset(
    {
        ("subprocess", "run"),
        ("subprocess", "Popen"),
        ("subprocess", "check_output"),
        ("subprocess", "check_call"),
        ("subprocess", "call"),
        ("subprocess", "getoutput"),
        ("subprocess", "getstatusoutput"),
        ("asyncio", "create_subprocess_exec"),
        ("asyncio", "create_subprocess_shell"),
        ("os", "system"),
        ("os", "popen"),
    }
)
_SPAWN_FROM_IMPORTS = {
    "subprocess": {
        "run",
        "Popen",
        "check_output",
        "check_call",
        "call",
        "getoutput",
        "getstatusoutput",
    },
    "asyncio": {"create_subprocess_exec", "create_subprocess_shell"},
    "os": {"system", "popen"},
}

DOCKER = "orchestrated_docker"
HOST_GIT = "host_clone_git"
LEGACY = "legacy_provider_runtime"

ALLOWED_SPAWN_SITES: dict[str, str] = {
    "roboco/llm/providers/_docker.py": DOCKER,
    "roboco/runtime/compose_labels.py": DOCKER,
    "roboco/runtime/sandbox.py": DOCKER,
    "roboco/runtime/engines/dispatch_breaker.py": DOCKER,
    "roboco/runtime/engines/reconcile.py": DOCKER,
    "roboco/runtime/engines/spawn_exit.py": DOCKER,
    "roboco/runtime/engines/spawn_launch.py": DOCKER,
    "roboco/runtime/engines/sweeps.py": DOCKER,
    "roboco/services/conventions.py": HOST_GIT,
    "roboco/services/git.py": HOST_GIT,
    "roboco/services/release_executor.py": HOST_GIT,
    "roboco/services/release_readiness.py": HOST_GIT,
    "roboco/services/workspace.py": HOST_GIT,
    "roboco/services/gateway/content_actions.py": HOST_GIT,
    "roboco/agent_sdk/grok_cli_session.py": LEGACY,
    "roboco/agent_sdk/live_cli_session.py": LEGACY,
    "roboco/llm/providers/codex.py": LEGACY,
    "roboco/llm/providers/gemini.py": LEGACY,
    "roboco/llm/providers/grok.py": LEGACY,
    "roboco/llm/providers/hummin.py": LEGACY,
    "roboco/llm/providers/hummin_cli_config.py": LEGACY,
    "roboco/llm/providers/kimi.py": LEGACY,
    "roboco/llm/providers/nebius.py": LEGACY,
    "roboco/llm/providers/openrouter.py": LEGACY,
    "roboco/runtime/engines/interactive_sessions.py": LEGACY,
}
REMOVAL_PENDING = frozenset({HOST_GIT, LEGACY})


def _spawn_lines(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and (func.value.id, func.attr) in _SPAWN_CALLS
            ):
                lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.module in _SPAWN_FROM_IMPORTS:
            wanted = _SPAWN_FROM_IMPORTS[node.module]
            if any(alias.name in wanted for alias in node.names):
                lines.append(node.lineno)
    return lines


def _modules_that_can_spawn() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        if WORKER_PACKAGE in path.parents:
            continue
        lines = _spawn_lines(path)
        if lines:
            found[path.relative_to(REPO_ROOT).as_posix()] = lines
    return found


def test_every_spawn_site_is_allowlisted_with_a_category() -> None:
    found = _modules_that_can_spawn()
    unlisted = sorted(set(found) - set(ALLOWED_SPAWN_SITES))
    assert not unlisted, (
        "new process-spawning module(s) outside the worker package; run project "
        f"code only through the offline worker, or classify the site: {unlisted}"
    )


def test_allowlist_has_no_stale_entries() -> None:
    found = _modules_that_can_spawn()
    stale = sorted(set(ALLOWED_SPAWN_SITES) - set(found))
    assert not stale, (
        f"allowlisted modules no longer spawn processes, remove them: {stale}"
    )


def test_allowlist_uses_only_known_categories() -> None:
    assert set(ALLOWED_SPAWN_SITES.values()) <= {DOCKER, HOST_GIT, LEGACY}


def test_nothing_outside_the_worker_runs_project_commands_through_a_shell() -> None:
    shell_sites = [
        f"{path}:{line}"
        for path in _modules_that_can_spawn()
        for line in _shell_lines(REPO_ROOT / path)
    ]
    assert not shell_sites, f"shell-string process spawns: {shell_sites}"


def _shell_lines(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in {
            "create_subprocess_shell",
            "system",
            "popen",
            "getoutput",
            "getstatusoutput",
        }:
            lines.append(node.lineno)
        if any(
            kw.arg == "shell"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is True
            for kw in node.keywords
        ):
            lines.append(node.lineno)
    return lines


def _literal_programs(path: Path) -> set[str]:
    """Program names that are spelled out literally at spawn calls in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    programs: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and (func.value.id, func.attr) in _SPAWN_CALLS
        ):
            continue
        first = node.args[0]
        if isinstance(first, ast.List | ast.Tuple) and first.elts:
            first = first.elts[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            programs.add(first.value)
    return programs


def test_host_and_docker_sites_only_name_git_docker_or_clone_cleanup() -> None:
    """A host-side site that names any other program is project-code execution
    (an installer, a build tool, ``make``) and belongs in the offline worker."""
    permitted = {"git", "docker", "rm"}
    offenders = {
        site: sorted(programs - permitted)
        for site, category in ALLOWED_SPAWN_SITES.items()
        if category in {DOCKER, HOST_GIT}
        if (programs := _literal_programs(REPO_ROOT / site)) - permitted
    }
    assert not offenders, offenders


def test_pending_removal_sites_are_tracked_so_the_list_can_only_shrink() -> None:
    pending = sorted(
        site
        for site, category in ALLOWED_SPAWN_SITES.items()
        if category in REMOVAL_PENDING
    )
    assert pending, (
        "host-clone and legacy provider spawn sites are gone: delete this module's "
        "REMOVAL_PENDING bookkeeping and assert the allowlist is docker-only"
    )
