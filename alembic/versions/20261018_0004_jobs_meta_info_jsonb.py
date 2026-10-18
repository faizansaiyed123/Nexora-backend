"""Convert jobs.meta_info to JSONB for its GIN index.

The jobs model queries JSON fields and needs an index compatible with PostgreSQL.
The cast preserves existing JSON values.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261018_0004"
down_revision = "20261018_0003"
branch_labels = None
depends_on = None

INDEX_NAME = "idx_jobs_meta_info"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "jobs" not in set(inspector.get_table_names()):
        return

    columns = {column["name"]: column for column in inspector.get_columns("jobs")}
    if "meta_info" not in columns:
        return

    column_type = columns["meta_info"]["type"]
    is_jsonb = isinstance(column_type, postgresql.JSONB)
    indexes = {index["name"]: index for index in inspector.get_indexes("jobs")}
    existing_index = indexes.get(INDEX_NAME)
    existing_index_is_gin = (
        existing_index is not None
        and existing_index.get("dialect_options", {}).get("postgresql_using") == "gin"
    )

    if existing_index is not None and (not is_jsonb or not existing_index_is_gin):
        op.drop_index(INDEX_NAME, table_name="jobs")
        existing_index = None

    if not is_jsonb:
        op.alter_column(
            "jobs",
            "meta_info",
            existing_type=column_type,
            type_=postgresql.JSONB(),
            existing_nullable=columns["meta_info"]["nullable"],
            postgresql_using="meta_info::jsonb",
        )

    if existing_index is None:
        op.create_index(
            INDEX_NAME,
            "jobs",
            ["meta_info"],
            postgresql_using="gin",
        )


def downgrade() -> None:
    # Keep JSONB and the GIN index; converting back to JSON would invalidate it.
    pass
