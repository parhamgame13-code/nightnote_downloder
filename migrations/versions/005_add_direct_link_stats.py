"""add direct_link_token/url/expires_at/hit_count for renewal + view-count stats

Revision ID: 005
Revises: 004
Create Date: 2026-08-08 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "download_jobs",
        sa.Column("direct_link_token", sa.String(64), nullable=True),
    )
    op.add_column(
        "download_jobs",
        sa.Column("direct_link_url", sa.String(2048), nullable=True),
    )
    op.add_column(
        "download_jobs",
        sa.Column("direct_link_expires_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "download_jobs",
        sa.Column("direct_link_hit_count", sa.Integer(), nullable=True, server_default="0"),
    )
    op.create_index(
        "ix_download_jobs_direct_link_token", "download_jobs", ["direct_link_token"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_download_jobs_direct_link_token", table_name="download_jobs")
    op.drop_column("download_jobs", "direct_link_hit_count")
    op.drop_column("download_jobs", "direct_link_expires_at")
    op.drop_column("download_jobs", "direct_link_url")
    op.drop_column("download_jobs", "direct_link_token")
