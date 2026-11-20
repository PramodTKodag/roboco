"""roboco/services/optimal_brain/availability.py

One gate for the semantic knowledge base. Embeddings and answer synthesis need
a model server, which the subscription-only policy keeps out of the runtime, so
every entry point asks here instead of reaching for a client.
"""

from __future__ import annotations

from roboco.config import settings
from roboco.services.base import FeatureUnavailableError

SEMANTIC_KB_FEATURE = "semantic_kb"


def semantic_kb_available() -> bool:
    """True only when the policy lets the semantic knowledge base run."""
    return settings.semantic_kb_enabled


def require_semantic_kb() -> None:
    """Raise ``FeatureUnavailableError`` unless the semantic KB may run."""
    if not semantic_kb_available():
        raise FeatureUnavailableError(
            SEMANTIC_KB_FEATURE,
            "semantic search and indexing are disabled by the subscription-only "
            "policy; use repository file search instead",
        )
