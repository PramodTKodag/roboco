"""The release acceptance matrix: every required negative case has live proof.

Static part: each matrix row names tests that exist. Live part: against a
running personal stack (skipped when none is up), `verify_boundary` inspects the
actual containers, networks, mounts and edge, never source files.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration.subscription.docker_support import (
    REPO_ROOT,
    docker,
    docker_unavailable_reason,
)
from tests.security.subscription.acceptance_matrix import MATRIX, NOT_PROVEN_BY_CODE

TESTS = REPO_ROOT / "tests"
STACK_PROJECT = "roboco-personal"
REQUIRED_ROWS = 20


def _test_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_")
    }


def test_the_matrix_covers_every_required_row() -> None:
    assert len(MATRIX) == REQUIRED_ROWS
    for row, evidence in MATRIX.items():
        assert evidence, f"{row}: no evidence"
        assert len(set(evidence)) == len(evidence), row


@pytest.mark.parametrize("row", sorted(MATRIX))
def test_every_matrix_row_names_tests_that_exist(row: str) -> None:
    missing = []
    for evidence in MATRIX[row]:
        relative, _, name = evidence.partition("::")
        path = TESTS / relative
        if not path.is_file() or name not in _test_names(path):
            missing.append(evidence)
    assert missing == [], row


def test_the_unproven_items_are_stated() -> None:
    assert NOT_PROVEN_BY_CODE


def test_the_quickstart_script_refuses_to_start_the_legacy_stack() -> None:
    result = subprocess.run(  # noqa: S603  # nosec B603
        ["bash", str(REPO_ROOT / "scripts" / "bootstrap.sh")],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env={"PATH": os.environ["PATH"]},
    )
    assert result.returncode == 2
    assert "make personal-up" in result.stderr


def _running_stack() -> bool:
    if docker_unavailable_reason() is not None:
        return False
    listing = docker(
        "ps", "--filter", f"label=com.docker.compose.project={STACK_PROJECT}", "--quiet",
        check=False,
    ).stdout  # fmt: skip
    return bool(listing.split())


def _load_verifier() -> object:
    scripts = REPO_ROOT / "scripts" / "personal"
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location(
            "verify_boundary_live", scripts / "verify_boundary.py"
        )
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(scripts))
    return module


@pytest.mark.integration
@pytest.mark.skipif(not _running_stack(), reason="the personal stack is not running")
def test_the_running_stack_passes_every_boundary_check() -> None:
    verifier = _load_verifier()
    results = verifier.run_checks(STACK_PROJECT)  # type: ignore[attr-defined]
    failed = [(check.name, check.detail) for check in results if not check.passed]
    assert failed == []
    assert len(results) >= 20
