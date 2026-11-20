"""The per-workspace ``.uv-python`` managed-CPython directory stays reachable.

An agent running ``uv`` inside its own container may fetch a managed CPython into
``<clone>/.uv-python``. The ownership walk must not prune it (so the agent can
traverse it) and the repo must gitignore it (so it is never committed).
"""

from __future__ import annotations

from pathlib import Path as _Path

from roboco.services.workspace import _PRUNE_DIRS

# ---------------------------------------------------------------------------
# _PRUNE_DIRS — the chown walk must reach .uv-python
# ---------------------------------------------------------------------------


def test_uv_python_install_dir_not_pruned() -> None:
    # If .uv-python were pruned, _ensure_agent_owned would never chown the
    # managed CPython and the agent still couldn't traverse it.
    assert ".uv-python" not in _PRUNE_DIRS


def test_repo_gitignore_ignores_uv_python_dir() -> None:
    # The per-workspace managed-CPython dir lives inside the clone (and thus
    # inside every worktree checkout). It must be gitignored so an agent never
    # commits a multi-GB CPython fetch.
    gitignore = _Path(__file__).resolve().parents[3] / ".gitignore"
    assert gitignore.exists(), f".gitignore not found at {gitignore}"
    lines = gitignore.read_text().splitlines()
    assert ".uv-python/" in lines, ".uv-python/ must be gitignored (now per-workspace)"
