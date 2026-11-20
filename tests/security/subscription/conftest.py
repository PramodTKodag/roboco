"""Reuse the real-Docker fixtures of the subscription integration suite."""

from tests.integration.subscription.conftest import (  # noqa: F401 - fixture re-export
    created_volumes,
    worker_image,
)
