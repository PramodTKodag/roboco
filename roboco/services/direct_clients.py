"""roboco/services/direct_clients.py

The one gate in front of every direct model client (an OpenAI-compatible chat
endpoint called by the controller itself). Model work runs through the trusted
subscription runtime only, so these clients stay locked off; each call site
asks here before it opens a connection.
"""

from __future__ import annotations

from roboco.config import settings
from roboco.services.base import FeatureUnavailableError

DIRECT_MODEL_CLIENT_FEATURE = "direct_model_client"


def require_direct_model_clients() -> None:
    """Raise ``FeatureUnavailableError`` unless direct model clients are allowed."""
    if not settings.direct_model_clients_enabled:
        raise FeatureUnavailableError(
            DIRECT_MODEL_CLIENT_FEATURE,
            "model calls outside the subscription runtime are disabled by the "
            "subscription-only policy",
        )
