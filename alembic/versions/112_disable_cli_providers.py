"""Disable every non-Anthropic provider until the operator enables it.

Grok, Codex, Gemini, Kimi, Nebius, ZAI, Ollama Cloud and the self-hosted
(Ollama) provider stay in the codebase, but none of them is routable until the
operator enables it, after its own isolation contract has been proven.
Earlier migrations seeded several of these rows as enabled; this migration
turns them off. Rows, keys and ``model_assignments`` are left in
place, so enabling a provider again is a single settings change. An agent whose
assignment points to a disabled provider fails routing explicitly instead of
moving to another provider.

Revision ID: 112_disable_cli_providers
Revises: 111_remove_openrouter_hummin
Create Date: 2026-11-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "112_disable_cli_providers"
down_revision = "111_remove_openrouter_hummin"
branch_labels = None
depends_on = None

_DISABLED_PROVIDER_TYPES = (
    "grok",
    "openai",
    "gemini",
    "kimi",
    "nebius",
    "zai",
    "ollama_cloud",
    "local",
)


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            "UPDATE provider_configs SET enabled = false "
            "WHERE type::text = ANY(:provider_types)"
        ),
        {"provider_types": list(_DISABLED_PROVIDER_TYPES)},
    )


def downgrade() -> None:
    # The previous enabled state is not recorded; providers stay disabled.
    pass
