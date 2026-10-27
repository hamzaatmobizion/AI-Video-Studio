"""portraits: character portraits Gemini draws

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "portraits",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer),
        sa.Column("job_id", sa.String(64)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("image_path", sa.String(500)),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_portraits_user", ondelete="CASCADE"),
    )
    op.create_index("ix_portraits_user_id", "portraits", ["user_id"])


def downgrade():
    op.drop_index("ix_portraits_user_id", table_name="portraits")
    op.drop_table("portraits")
