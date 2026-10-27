"""remake one scene, music track, locked first frames and clip checks

Revision ID: 0008
Revises: 0007
"""
import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("generations") as b:
        b.add_column(sa.Column("music_path", sa.String(500)))
        b.add_column(sa.Column("lock_frames", sa.Boolean, nullable=False, server_default=sa.false()))
        b.add_column(sa.Column("check_clips", sa.Boolean, nullable=False, server_default=sa.false()))
        b.add_column(sa.Column("old_drive_file_id", sa.String(128)))
        b.add_column(sa.Column("job_kind", sa.String(20), nullable=False, server_default=""))
    with op.batch_alter_table("scenes") as b:
        b.add_column(sa.Column("still_path", sa.String(500)))
        b.add_column(sa.Column("check_note", sa.Text, nullable=False, server_default=""))
        b.add_column(sa.Column("redos", sa.Integer, nullable=False, server_default="0"))


def downgrade():
    with op.batch_alter_table("scenes") as b:
        for name in ("still_path", "check_note", "redos"):
            b.drop_column(name)
    with op.batch_alter_table("generations") as b:
        for name in ("music_path", "lock_frames", "check_clips", "old_drive_file_id", "job_kind"):
            b.drop_column(name)
