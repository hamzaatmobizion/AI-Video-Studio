"""where each video's clips are made: Gemini or Google Flow

Revision ID: 0009
Revises: 0008
"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("generations") as b:
        b.add_column(sa.Column("provider", sa.String(20), nullable=False, server_default="gemini"))


def downgrade():
    with op.batch_alter_table("generations") as b:
        b.drop_column("provider")
