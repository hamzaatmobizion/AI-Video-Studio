"""Why the hand bot stopped on a by-hand video (shown on its page and card until it's tried again)

Revision ID: 0012
Revises: 0011
"""
import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("generations") as b:
        b.add_column(sa.Column("bot_note", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("generations") as b:
        b.drop_column("bot_note")
