"""add per-user settings columns (custom_thumbnail_file_id, spoiler_effect,
upload_as_document, timezone) for the new /settings menu.

idempotent مثل migration 006، چون create_all هم موازیش هست.

Revision ID: 007
Revises: 006
Create Date: 2026-08-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return column in {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    if not _has_column("users", "custom_thumbnail_file_id"):
        op.add_column("users", sa.Column("custom_thumbnail_file_id", sa.String(255), nullable=True))
    if not _has_column("users", "spoiler_effect"):
        op.add_column("users", sa.Column("spoiler_effect", sa.Boolean(), server_default=sa.false(), nullable=True))
    if not _has_column("users", "upload_as_document"):
        op.add_column("users", sa.Column("upload_as_document", sa.Boolean(), server_default=sa.false(), nullable=True))
    if not _has_column("users", "timezone"):
        op.add_column("users", sa.Column("timezone", sa.String(64), nullable=True))
    if not _has_column("users", "show_rename_option"):
        op.add_column("users", sa.Column("show_rename_option", sa.Boolean(), server_default=sa.false(), nullable=True))
    if not _has_column("users", "receive_screenshots"):
        op.add_column("users", sa.Column("receive_screenshots", sa.Boolean(), nullable=True))
    if not _has_column("download_jobs", "raw_domain"):
        op.add_column("download_jobs", sa.Column("raw_domain", sa.String(255), nullable=True))
        op.create_index("ix_download_jobs_raw_domain", "download_jobs", ["raw_domain"])


def downgrade() -> None:
    if _has_column("download_jobs", "raw_domain"):
        op.drop_index("ix_download_jobs_raw_domain", table_name="download_jobs")
        op.drop_column("download_jobs", "raw_domain")
    cols = (
        "custom_thumbnail_file_id", "spoiler_effect", "upload_as_document",
        "timezone", "show_rename_option", "receive_screenshots",
    )
    for col in cols:
        if _has_column("users", col):
            op.drop_column("users", col)
