"""add playlist_total column (existed on the SQLAlchemy model but was never
actually migrated - db/models.py DownloadJob.playlist_total had no matching
Alembic revision, so on a fresh Postgres install it would raise
UndefinedColumn the moment anything tried to write it). Needed now for the
aggregate playlist/album progress reporting feature.

این migration عمداً idempotent نوشته شده: چون این پروژه علاوه بر Alembic،
موقع استارت هم از Base.metadata.create_all استفاده می‌کنه (db/session.py)،
رو دیتابیس‌های موجود (مخصوصاً SQLite که از قبل اجرا شدن) این ستون ممکنه
از قبل - مستقیم از مدل، نه از یه migration - ساخته شده باشه. بدون این چک،
اجرای مستقیم ALTER TABLE رو همچین دیتابیسی با خطای
«duplicate column name: playlist_total» fail می‌شه.

Revision ID: 006
Revises: 005
Create Date: 2026-08-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return column in {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    if not _has_column("download_jobs", "playlist_total"):
        op.add_column(
            "download_jobs",
            sa.Column("playlist_total", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    if _has_column("download_jobs", "playlist_total"):
        op.drop_column("download_jobs", "playlist_total")
