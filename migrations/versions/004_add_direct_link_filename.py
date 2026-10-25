"""add direct_link_filename column for custom english direct-link names

Revision ID: 004
Revises: 003
Create Date: 2026-08-05 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "download_jobs",
        sa.Column("direct_link_filename", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("download_jobs", "direct_link_filename")
