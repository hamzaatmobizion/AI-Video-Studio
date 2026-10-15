"""per-user generations and Drive connections

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("generations") as b:
        b.add_column(sa.Column("user_id", sa.Integer))
        b.create_foreign_key("fk_generations_user", "users", ["user_id"], ["id"], ondelete="CASCADE")
        b.create_index("ix_generations_user_id", ["user_id"])
    with op.batch_alter_table("google_connection") as b:
        b.add_column(sa.Column("user_id", sa.Integer))
        b.create_foreign_key("fk_google_connection_user", "users", ["user_id"], ["id"], ondelete="CASCADE")
        b.create_unique_constraint("uq_google_connection_user", ["user_id"])
    # Rows from the single-owner era belong to the first account.
    first = "(SELECT MIN(id) FROM users)"
    op.execute(f"UPDATE generations SET user_id = {first} WHERE user_id IS NULL")
    op.execute(f"UPDATE google_connection SET user_id = {first} WHERE user_id IS NULL")


def downgrade():
    with op.batch_alter_table("google_connection") as b:
        b.drop_constraint("uq_google_connection_user", type_="unique")
        b.drop_constraint("fk_google_connection_user", type_="foreignkey")
        b.drop_column("user_id")
    with op.batch_alter_table("generations") as b:
        b.drop_index("ix_generations_user_id")
        b.drop_constraint("fk_generations_user", type_="foreignkey")
        b.drop_column("user_id")
