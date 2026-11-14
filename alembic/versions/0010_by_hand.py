"""Flow videos made by hand: the user makes each clip in Google Flow and uploads it

Revision ID: 0010
Revises: 0009
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("generations") as b:
        b.add_column(sa.Column("by_hand", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    with op.batch_alter_table("generations") as b:
        b.drop_column("by_hand")
