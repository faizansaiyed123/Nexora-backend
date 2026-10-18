"""Convert offering attributes to JSONB for the GIN index.

Existing databases may have been bootstrapped before the model was corrected.
The conversion preserves existing JSON values.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261018_0003"
down_revision = "20261003_0002"
branch_labels = None
depends_on = None

INDEX_NAME = "idx_offerings_attributes_gin"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "offerings" not in tables:
        return

    columns = {column["name"]: column for column in inspector.get_columns("offerings")}
    if "attributes" not in columns:
        return

    indexes = {index["name"] for index in inspector.get_indexes("offerings")}
    column_type = columns["attributes"]["type"]
    is_jsonb = isinstance(column_type, postgresql.JSONB)

    if not is_jsonb:
        if INDEX_NAME in indexes:
            op.drop_index(INDEX_NAME, table_name="offerings")
        op.alter_column(
            "offerings",
            "attributes",
            existing_type=column_type,
            type_=postgresql.JSONB(),
            existing_nullable=False,
            postgresql_using="attributes::jsonb",
        )

    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("offerings")}
    if INDEX_NAME not in indexes:
        op.create_index(
            INDEX_NAME,
            "offerings",
            ["attributes"],
            postgresql_using="gin",
        )


def downgrade() -> None:
    # Keep JSONB and its index on downgrade: converting back to JSON would
    # make the GIN index invalid and can break existing query behavior.
    pass
