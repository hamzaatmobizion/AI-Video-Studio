"""generations.planned: Gemini wrote the scenes from one prompt

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("generations") as b:
        b.add_column(sa.Column("planned", sa.Boolean, nullable=False, server_default=sa.false()))


def downgrade():
    with op.batch_alter_table("generations") as b:
        b.drop_column("planned")
