"""Local review workflow: patch artifacts and the task workflow marker.

Strict-profile deployments never open pull requests. A task's work is captured
as a local patch artifact (stored text plus its SHA-256), reviewed by QA and the
owner, and exported by the owner. ``tasks.review_workflow`` records which
workflow a task uses; rows that already carry a pull request keep it as read-only
history under the ``forge`` workflow. Every other row is ``local_review``.

Revision ID: 113_local_review_artifacts
Revises: 112_disable_cli_providers
Create Date: 2026-11-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "113_local_review_artifacts"
down_revision = "112_disable_cli_providers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("captured_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("patch", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("paths", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_review_artifacts_task_id", "review_artifacts", ["task_id"])
    op.create_index(
        "ix_review_artifacts_task_status", "review_artifacts", ["task_id", "status"]
    )
    op.add_column(
        "tasks",
        sa.Column(
            "review_workflow",
            sa.String(20),
            nullable=False,
            server_default="local_review",
        ),
    )
    op.add_column(
        "tasks",
        sa.Column("review_artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_tasks_review_artifact_id",
        "tasks",
        "review_artifacts",
        ["review_artifact_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute("UPDATE tasks SET review_workflow = 'forge' WHERE pr_number IS NOT NULL")


def downgrade() -> None:
    op.drop_constraint("fk_tasks_review_artifact_id", "tasks", type_="foreignkey")
    op.drop_column("tasks", "review_artifact_id")
    op.drop_column("tasks", "review_workflow")
    op.drop_index("ix_review_artifacts_task_status", table_name="review_artifacts")
    op.drop_index("ix_review_artifacts_task_id", table_name="review_artifacts")
    op.drop_table("review_artifacts")
