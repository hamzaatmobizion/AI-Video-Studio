"""The script file (in the scripts folder) a video was made from, by the Scripts tab

Revision ID: 0015
Revises: 0014
"""
import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("generations") as b:
        b.add_column(sa.Column("script_file", sa.String(255), nullable=True))


def downgrade():
    with op.batch_alter_table("generations") as b:
        b.drop_column("script_file")
