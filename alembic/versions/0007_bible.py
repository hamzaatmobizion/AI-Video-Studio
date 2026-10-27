"""video types and story bibles; locked traits for characters

Revision ID: 0007
Revises: 0006
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("generations") as b:
        b.add_column(sa.Column("genre", sa.String(30), nullable=False, server_default=""))
        b.add_column(sa.Column("bible", sa.JSON, nullable=False, server_default="{}"))
    with op.batch_alter_table("characters") as b:
        b.add_column(sa.Column("locked", sa.Text, nullable=False, server_default=""))
        b.add_column(sa.Column("never", sa.Text, nullable=False, server_default=""))


def downgrade():
    with op.batch_alter_table("characters") as b:
        b.drop_column("never")
        b.drop_column("locked")
    with op.batch_alter_table("generations") as b:
        b.drop_column("bible")
        b.drop_column("genre")
