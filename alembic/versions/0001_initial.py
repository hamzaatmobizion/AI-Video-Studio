"""initial tables

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_table(
        "generations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("prompt", sa.Text, nullable=False),
        sa.Column("aspect", sa.String(8), nullable=False),
        sa.Column("ref_images", sa.JSON, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, index=True),
        sa.Column("error", sa.Text),
        sa.Column("worker_job_id", sa.String(64)),
        sa.Column("final_prompt", sa.Text),
        sa.Column("submitted_at", sa.DateTime),
        sa.Column("poll_errors", sa.Integer, nullable=False),
        sa.Column("temp_path", sa.String(500)),
        sa.Column("upload_attempts", sa.Integer, nullable=False),
        sa.Column("next_upload_at", sa.DateTime),
        sa.Column("drive_file_id", sa.String(128)),
        sa.Column("file_size", sa.Integer),
        sa.Column("dismissed", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )
    op.create_table(
        "google_connection",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("refresh_token_enc", sa.Text, nullable=False),
        sa.Column("email", sa.String(320)),
        sa.Column("folder_id", sa.String(128)),
        sa.Column("folder_name", sa.String(255)),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )


def downgrade():
    op.drop_table("google_connection")
    op.drop_table("generations")
    op.drop_table("users")
