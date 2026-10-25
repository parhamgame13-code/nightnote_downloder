"""add description column for full captions

Revision ID: 002
Revises: 001
Create Date: 2026-07-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("download_jobs", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("file_cache", sa.Column("description", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("file_cache", "description")
    op.drop_column("download_jobs", "description")
