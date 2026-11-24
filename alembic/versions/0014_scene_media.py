"""Each scene's own start picture and sound effect, and saved series (a Flow form to refill for the next episode)

Revision ID: 0014
Revises: 0013
"""
import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("scenes") as b:
        b.add_column(sa.Column("start_path", sa.String(500), nullable=True))
        b.add_column(sa.Column("sound_path", sa.String(500), nullable=True))
    op.create_table(
        "series",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("prompt", sa.Text, nullable=False),
        sa.Column("aspect", sa.String(8), nullable=False),
        sa.Column("scenes", sa.Integer, nullable=False),
        sa.Column("cast_ids", sa.JSON, nullable=False),
        sa.Column("style", sa.Text, nullable=False),
        sa.Column("language", sa.String(40), nullable=False),
        sa.Column("genre", sa.String(30), nullable=False),
        sa.Column("review", sa.Boolean, nullable=False),
        sa.Column("lock_frames", sa.Boolean, nullable=False),
        sa.Column("check_clips", sa.Boolean, nullable=False),
        sa.Column("by_hand", sa.Boolean, nullable=False),
        sa.Column("music_path", sa.String(500)),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_series_user", ondelete="CASCADE"),
    )
    op.create_index("ix_series_user_id", "series", ["user_id"])


def downgrade():
    op.drop_index("ix_series_user_id", "series")
    op.drop_table("series")
    with op.batch_alter_table("scenes") as b:
        b.drop_column("sound_path")
        b.drop_column("start_path")
