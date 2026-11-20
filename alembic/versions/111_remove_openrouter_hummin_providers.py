"""Remove the OpenRouter and Hummin providers.

Both providers authenticate with a metered API key and are no longer part of
the platform. Their ``provider_configs`` rows (and the ``model_assignments``
that reference them, which would otherwise block the delete through the
``RESTRICT`` foreign key) are deleted, along with the persisted Decisions
OpenRouter tier flag.

The ``'openrouter'`` and ``'hummin'`` labels stay in the Postgres
``modelprovider`` enum: Postgres cannot drop an enum label in place, and with
no row using them they are inert. Migrations 095, 096, 101 and 102 are
unchanged history.

Revision ID: 111_remove_openrouter_hummin
Revises: 110_project_offline_workspace
Create Date: 2026-11-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "111_remove_openrouter_hummin"
down_revision = "110_project_offline_workspace"
branch_labels = None
depends_on = None

_REMOVED_PROVIDER_TYPES = ("openrouter", "hummin")
_REMOVED_SETTING_KEYS = ("decisions_tier_openrouter_enabled",)


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "DELETE FROM model_assignments WHERE provider_config_id IN ("
            "    SELECT id FROM provider_configs "
            "    WHERE type::text = ANY(:provider_types)"
            ")"
        ),
        {"provider_types": list(_REMOVED_PROVIDER_TYPES)},
    )
    connection.execute(
        sa.text("DELETE FROM provider_configs WHERE type::text = ANY(:provider_types)"),
        {"provider_types": list(_REMOVED_PROVIDER_TYPES)},
    )
    connection.execute(
        sa.text("DELETE FROM system_settings WHERE key = ANY(:setting_keys)"),
        {"setting_keys": list(_REMOVED_SETTING_KEYS)},
    )


def downgrade() -> None:
    # The removed rows carried operator-supplied keys and assignments that
    # cannot be reconstructed; downgrading leaves the providers absent.
    pass
