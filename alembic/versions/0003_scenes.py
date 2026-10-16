"""scenes: a generation is one or more ~10 s clips joined together

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "scenes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("generation_id", sa.Integer, nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("prompt", sa.Text, nullable=False),
        sa.Column("clip_path", sa.String(500)),
        sa.ForeignKeyConstraint(["generation_id"], ["generations.id"], name="fk_scenes_generation",
                                ondelete="CASCADE"),
        sa.UniqueConstraint("generation_id", "position", name="uq_scenes_generation_position"),
    )
    op.create_index("ix_scenes_generation_id", "scenes", ["generation_id"])
    # Every existing video was a single clip: give it scene 1 (in-flight ones keep their worker job).
    op.execute("INSERT INTO scenes (generation_id, position, prompt) SELECT id, 1, prompt FROM generations")


def downgrade():
    op.drop_index("ix_scenes_generation_id", table_name="scenes")
    op.drop_table("scenes")
