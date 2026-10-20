"""characters, and a structured script per scene (setting, shot, who is in it, dialogue)

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "characters",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("appearance", sa.Text, nullable=False, server_default=""),
        sa.Column("outfit", sa.Text, nullable=False, server_default=""),
        sa.Column("voice", sa.Text, nullable=False, server_default=""),
        sa.Column("personality", sa.Text, nullable=False, server_default=""),
        sa.Column("image_path", sa.String(500)),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_characters_user", ondelete="CASCADE"),
    )
    op.create_index("ix_characters_user_id", "characters", ["user_id"])
    with op.batch_alter_table("generations") as b:
        b.add_column(sa.Column("review", sa.Boolean, nullable=False, server_default=sa.false()))
        b.add_column(sa.Column("cast_ids", sa.JSON, nullable=False, server_default="[]"))
        b.add_column(sa.Column("style", sa.Text, nullable=False, server_default=""))
        b.add_column(sa.Column("language", sa.String(40), nullable=False, server_default=""))
    with op.batch_alter_table("scenes") as b:
        for name in ("setting", "action", "sound"):
            b.add_column(sa.Column(name, sa.Text, nullable=False, server_default=""))
        b.add_column(sa.Column("time_of_day", sa.String(60), nullable=False, server_default=""))
        b.add_column(sa.Column("shot", sa.JSON, nullable=False, server_default="{}"))
        b.add_column(sa.Column("characters", sa.JSON, nullable=False, server_default="[]"))
        b.add_column(sa.Column("dialogue", sa.JSON, nullable=False, server_default="[]"))


def downgrade():
    with op.batch_alter_table("scenes") as b:
        for name in ("setting", "action", "sound", "time_of_day", "shot", "characters", "dialogue"):
            b.drop_column(name)
    with op.batch_alter_table("generations") as b:
        for name in ("review", "cast_ids", "style", "language"):
            b.drop_column(name)
    op.drop_index("ix_characters_user_id", table_name="characters")
    op.drop_table("characters")
