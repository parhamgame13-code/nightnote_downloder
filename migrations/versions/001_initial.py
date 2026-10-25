"""initial

Revision ID: 001
Revises:
Create Date: 2025-01-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(128), nullable=True),
        sa.Column("full_name", sa.String(256), nullable=False),
        sa.Column("is_banned", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("daily_download_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_download_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_id"),
    )
    op.create_index("ix_users_telegram_id", "users", ["telegram_id"])

    op.create_table(
        "download_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("detected_site", sa.String(64), nullable=False, server_default="unknown"),
        sa.Column("selected_quality", sa.String(32), nullable=False, server_default="best"),
        sa.Column("status", sa.Enum("pending", "downloading", "uploading", "done", "failed", "cancelled", name="downloadstatus"), nullable=False, server_default="pending"),
        sa.Column("file_name", sa.String(512), nullable=True),
        sa.Column("file_size_mb", sa.Float(), nullable=True),
        sa.Column("error_msg", sa.Text(), nullable=True),
        sa.Column("celery_task_id", sa.String(128), nullable=True),
        sa.Column("playlist_id", sa.String(64), nullable=True),
        sa.Column("playlist_index", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_download_jobs_user_id", "download_jobs", ["user_id"])
    op.create_index("ix_download_jobs_playlist_id", "download_jobs", ["playlist_id"])

    op.create_table(
        "file_cache",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("url_hash", sa.String(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("quality", sa.String(32), nullable=False),
        sa.Column("telegram_file_id", sa.String(256), nullable=False),
        sa.Column("file_type", sa.String(16), nullable=False),
        sa.Column("file_name", sa.String(512), nullable=False),
        sa.Column("file_size_mb", sa.Float(), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url_hash"),
    )
    op.create_index("ix_file_cache_url_hash", "file_cache", ["url_hash"])

    op.create_table(
        "bot_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("label_fa", sa.String(256), nullable=False, server_default=""),
        sa.Column("value_type", sa.String(16), nullable=False, server_default="str"),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )
    op.create_index("ix_bot_config_key", "bot_config", ["key"])


def downgrade() -> None:
    op.drop_table("bot_config")
    op.drop_table("file_cache")
    op.drop_table("download_jobs")
    op.drop_table("users")
