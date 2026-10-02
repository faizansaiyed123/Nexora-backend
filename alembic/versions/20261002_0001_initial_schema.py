"""Bootstrap the current SQLAlchemy metadata as the initial Nexora schema.

revision = 20261002_0001
down_revision = None
"""

from alembic import op
import backend.models
from backend.db.base import Base

revision = "20261002_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
