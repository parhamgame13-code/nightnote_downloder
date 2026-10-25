"""drop UNIQUE constraint on download_jobs.telegram_file_id

file_id تلگرام برای یه فایل ثابته؛ دو job مختلف که همزمان همون مدیا رو
می‌فرستن file_id یکسان می‌گیرن. با UNIQUE، آپدیتِ DONE دومی IntegrityError
می‌خورد و job دوم با وجود ارسال موفق فایل، FAILED ثبت می‌شد.

نکته برای SQLite: constraint یونیک ستونی تو SQLite یه autoindex می‌سازه که
با DROP INDEX قابل حذف نیست (بازسازی جدول لازم می‌شه). برای SQLite این
migration عمداً no-op می‌مونه - مدل‌ها unique=False شدن (دیتابیس‌های جدید
درست ساخته می‌شن) و send_to_telegram هم IntegrityError رو مدیریت می‌کنه،
پس رفتار روی هر دو نوع دیتابیس درسته. برای PostgreSQL که constraint
نام‌دار داره، اینجا واقعاً drop می‌شه.

Revision ID: 008
Revises: 007
Create Date: 2026-10-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # autoindex قابل drop نیست؛ ببین docstring بالای فایل
        return
    inspector = sa.inspect(bind)
    for uq in inspector.get_unique_constraints("download_jobs"):
        columns = set(uq.get("column_names") or [])
        name = uq.get("name")
        if columns == {"telegram_file_id"} and name:
            op.drop_constraint(name, "download_jobs", type_="unique")
            return


def downgrade() -> None:
    # برگردوندن unique اصلاً درست نیست (همون باگ false-failure رو برمی‌گردونه)
    # - عمداً no-op.
    pass
