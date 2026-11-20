"""``roboco.runtime`` keeps its public names but stays light to import."""

from __future__ import annotations

import subprocess
import sys

import pytest
from roboco import runtime


def test_public_names_resolve_lazily() -> None:
    for name in runtime.__all__:
        assert getattr(runtime, name) is not None


def test_unknown_name_raises_attribute_error() -> None:
    with pytest.raises(AttributeError):
        runtime.DoesNotExist  # noqa: B018


def test_importing_the_subscription_package_does_not_load_the_orchestrator() -> None:
    code = (
        "import sys, roboco.runtime.subscription.contracts;"
        "print('roboco.runtime.orchestrator' in sys.modules)"
    )
    output = subprocess.run(
        [sys.executable, "-P", "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert output == "False"
