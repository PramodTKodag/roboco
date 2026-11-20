"""ZAI provider wiring: catalog membership, mode mapping, enum values.

The ZAI provider (Z.ai GLM family) rides the built-in Claude Code spawn via
ANTHROPIC_BASE_URL injection. It keeps its row and key endpoint but has no
static-catalog entries: the GLM catalog entries are the Ollama cloud tags.
These tests pin that surface so a refactor cannot silently change it.
"""

from __future__ import annotations

from roboco.models.base import ModelProvider
from roboco.models.llm_catalog import MODEL_CATALOG_BY_NAME, provider_type_for_model
from roboco.services.llm import _SINGLE_GLOBAL_MODE_BY_PROVIDER


def test_zai_enum_value() -> None:
    assert ModelProvider.ZAI.value == "zai"
    assert ModelProvider("zai") is ModelProvider.ZAI


def test_glm_catalog_entries_are_ollama_cloud_tags() -> None:
    assert provider_type_for_model("glm-5.3:cloud") is ModelProvider.OLLAMA_CLOUD
    assert provider_type_for_model("glm-5.3-flash:cloud") is ModelProvider.OLLAMA_CLOUD


def test_zai_has_no_static_catalog_entries() -> None:
    zai_entries = [
        name
        for name, entry in MODEL_CATALOG_BY_NAME.items()
        if entry.provider_type is ModelProvider.ZAI
    ]
    assert zai_entries == []


def test_zai_mode_mapping() -> None:
    assert _SINGLE_GLOBAL_MODE_BY_PROVIDER[ModelProvider.ZAI] == "zai"
