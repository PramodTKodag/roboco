"""Link a project to its imported offline workspace.

Model runs read and change project files only inside an offline worker
workspace the owner imported. This column holds that workspace's handle, so a
delivery run can be bound to exactly one workspace of exactly its project.
Purely additive; null means no workspace has been imported.

Revision ID: 110_project_offline_workspace
Revises: 109_memory_links
Create Date: 2026-11-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "110_project_offline_workspace"
down_revision = "109_memory_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("offline_workspace_handle", sa.UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("projects", "offline_workspace_handle")
