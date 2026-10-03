"""Add execution leases, idempotent scheduled-job invariant, and notification outbox.

This migration is intentionally conditional because the repository's original
bootstrap migration calls Base.metadata.create_all(). Fresh installations may
already contain the new model metadata before this migration runs.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "20261003_0002"
down_revision = "20261002_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    job_columns = {c["name"] for c in inspector.get_columns("jobs")} if "jobs" in tables else set()
    if "lease_expires_at" not in job_columns:
        op.add_column("jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
        op.create_index("ix_jobs_lease_expires_at", "jobs", ["lease_expires_at"])
    elif "ix_jobs_lease_expires_at" not in {i["name"] for i in inspector.get_indexes("jobs")}:
        op.create_index("ix_jobs_lease_expires_at", "jobs", ["lease_expires_at"])

    job_columns = {c["name"] for c in inspector.get_columns("jobs")}
    if "lease_token" not in job_columns:
        op.add_column("jobs", sa.Column("lease_token", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True))

    # Preserve one active scheduled job per match before adding the invariant.
    bind.execute(sa.text("""
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY (meta_info->>'offering_match_id')
                       ORDER BY created_at ASC, id ASC
                   ) AS rn
            FROM jobs
            WHERE job_type = 'SCHEDULED_CRAWL'
              AND status IN ('PENDING', 'RUNNING')
              AND (meta_info->>'offering_match_id') IS NOT NULL
        )
        UPDATE jobs
        SET status = 'FAILED',
            error_message = 'Duplicate scheduled job superseded during migration.',
            failed_items = 1,
            total_items_processed = 1,
            completed_at = NOW(),
            lease_expires_at = NULL
        WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
    """))

    indexes = {i["name"] for i in inspector.get_indexes("jobs")}
    if "uq_jobs_active_scheduled_match" not in indexes:
        op.create_index(
            "uq_jobs_active_scheduled_match",
            "jobs",
            [sa.text("(meta_info->>'offering_match_id')")],
            unique=True,
            postgresql_where=sa.text(
                "job_type = 'SCHEDULED_CRAWL' "
                "AND status IN ('PENDING', 'RUNNING') "
                "AND (meta_info->>'offering_match_id') IS NOT NULL"
            ),
        )

    if "notification_outbox" not in tables:
        op.create_table(
            "notification_outbox",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("client_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("channel", sa.String(length=30), nullable=False, server_default="email"),
            sa.Column("recipient", sa.String(length=320), nullable=False),
            sa.Column("title", sa.String(length=500), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("dedup_key", sa.String(length=500), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("dedup_key", name="uq_notification_outbox_dedup_key"),
        )
        op.create_index("ix_notification_outbox_client_id", "notification_outbox", ["client_id"])
        op.create_index("ix_notification_outbox_status", "notification_outbox", ["status"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "notification_outbox" in tables:
        op.drop_index("ix_notification_outbox_status", table_name="notification_outbox")
        op.drop_index("ix_notification_outbox_client_id", table_name="notification_outbox")
        op.drop_table("notification_outbox")

    if "jobs" in tables:
        indexes = {i["name"] for i in inspector.get_indexes("jobs")}
        if "uq_jobs_active_scheduled_match" in indexes:
            op.drop_index("uq_jobs_active_scheduled_match", table_name="jobs")
        columns = {c["name"] for c in inspector.get_columns("jobs")}
        if "lease_token" in columns:
            op.drop_column("jobs", "lease_token")
        if "ix_jobs_lease_expires_at" in indexes:
            op.drop_index("ix_jobs_lease_expires_at", table_name="jobs")
        if "lease_expires_at" in columns:
            op.drop_column("jobs", "lease_expires_at")
