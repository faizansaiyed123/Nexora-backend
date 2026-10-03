"""Add authorization versions, job leases/idempotency, and notification outbox.

Revision ID: 20261003_0002
Revises: 20261002_0001
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "20261003_0002"
down_revision = "20261002_0001"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    user_columns = _columns("users")
    if "auth_version" not in user_columns:
        op.add_column(
            "users",
            sa.Column("auth_version", sa.Integer(), nullable=False, server_default="0"),
        )
        op.alter_column("users", "auth_version", server_default=None)

    job_columns = _columns("jobs")
    if "idempotency_key" not in job_columns:
        op.add_column("jobs", sa.Column("idempotency_key", sa.String(length=255), nullable=True))
    if "lease_expires_at" not in job_columns:
        op.add_column("jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    if "attempt_count" not in job_columns:
        op.add_column("jobs", sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"))
        op.alter_column("jobs", "attempt_count", server_default=None)

    inspector = inspect(bind)
    indexes = {idx["name"] for idx in inspector.get_indexes("jobs")}
    if "ix_jobs_idempotency_key" not in indexes:
        op.create_index(
            "ix_jobs_idempotency_key",
            "jobs",
            ["idempotency_key"],
            unique=True,
        )
    if "ix_jobs_lease_expires_at" not in indexes:
        op.create_index(
            "ix_jobs_lease_expires_at",
            "jobs",
            ["lease_expires_at"],
            unique=False,
        )

    inspector = inspect(bind)
    if "notification_outbox" not in inspector.get_table_names():
        op.create_table(
            "notification_outbox",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("alert_log_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("recipient", sa.String(length=255), nullable=False),
            sa.Column("channel", sa.String(length=32), nullable=False, server_default="EMAIL"),
            sa.Column("subject", sa.String(length=500), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="PENDING"),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("dedupe_key", sa.String(length=255), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["alert_log_id"], ["alert_logs.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("dedupe_key", name="uq_notification_outbox_dedupe_key"),
        )
        op.create_index("ix_notification_outbox_alert_log_id", "notification_outbox", ["alert_log_id"])
        op.create_index("ix_notification_outbox_status", "notification_outbox", ["status"])
        op.create_index("ix_notification_outbox_next_attempt_at", "notification_outbox", ["next_attempt_at"])
        op.create_index("ix_notification_outbox_locked_until", "notification_outbox", ["locked_until"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "notification_outbox" in inspector.get_table_names():
        op.drop_table("notification_outbox")

    indexes = {idx["name"] for idx in inspect(bind).get_indexes("jobs")}
    if "ix_jobs_lease_expires_at" in indexes:
        op.drop_index("ix_jobs_lease_expires_at", table_name="jobs")
    if "ix_jobs_idempotency_key" in indexes:
        op.drop_index("ix_jobs_idempotency_key", table_name="jobs")

    columns = _columns("jobs")
    if "attempt_count" in columns:
        op.drop_column("jobs", "attempt_count")
    if "lease_expires_at" in columns:
        op.drop_column("jobs", "lease_expires_at")
    if "idempotency_key" in columns:
        op.drop_column("jobs", "idempotency_key")

    columns = _columns("users")
    if "auth_version" in columns:
        op.drop_column("users", "auth_version")
