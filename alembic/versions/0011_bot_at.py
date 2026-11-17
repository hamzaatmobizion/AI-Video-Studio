"""By-hand Flow videos handed to the hand bot: when "Create Video" was pressed

Revision ID: 0011
Revises: 0010
"""
import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("generations") as b:
        b.add_column(sa.Column("bot_at", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("generations") as b:
        b.drop_column("bot_at")
