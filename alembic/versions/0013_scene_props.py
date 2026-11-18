"""Each scene's objects: where each one is and who holds it, so every clip draws them the same way

Revision ID: 0013
Revises: 0012
"""
import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("scenes") as b:
        b.add_column(sa.Column("props", sa.JSON, nullable=False, server_default="[]"))


def downgrade():
    with op.batch_alter_table("scenes") as b:
        b.drop_column("props")
