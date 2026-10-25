"""
اسکریپت یک‌باره برای حذف ایندکس‌های تکراری از دیتابیس موجود.

توضیح: تو db/models.py سه تا مدل (User, FileCache, BotConfig) روی چند تا
ستون (telegram_id, created_at, url_hash, key) هم index=True داشتن هم یه
Index جدا با اسم دیگه تو __table_args__ - یعنی SQLAlchemy برای هر کدوم
دو تا ایندکس یکسان می‌ساخت. این تکراری‌ها از models.py حذف شدن، ولی چون
init_db() از Base.metadata.create_all() استفاده می‌کنه (نه Alembic)،
create_all() ایندکس‌های اضافه‌ای که قبلاً روی دیتابیس ساخته شده رو
خودکار پاک نمی‌کنه - برای همینه که این اسکریپت لازمه.

این اسکریپت امنه و idempotent هست: قبل از DROP هر ایندکس، چک می‌کنه که
واقعاً وجود داره یا نه (پس اجرای چندباره‌ش هیچ خطایی نمی‌ده).

استفاده (از مسیر ریشه‌ی پروژه):
    python scripts/drop_duplicate_indexes.py
"""
import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text
from db.session import engine

# (نام جدول، نام ایندکس تکراری‌ای که باید حذف بشه)
DUPLICATE_INDEXES = [
    ("users", "idx_user_telegram_id"),
    ("users", "idx_user_created_at"),
    ("file_cache", "idx_cache_url_hash"),
    ("file_cache", "idx_cache_created_at"),
    ("bot_config", "idx_config_key"),
]


async def main():
    async with engine.begin() as conn:
        existing = await conn.run_sync(
            lambda sync_conn: {
                (table, idx["name"])
                for table in {t for t, _ in DUPLICATE_INDEXES}
                for idx in inspect(sync_conn).get_indexes(table)
            }
        )

        dropped = 0
        for table, index_name in DUPLICATE_INDEXES:
            if (table, index_name) not in existing:
                print(f"⏭  {index_name} روی {table} از قبل وجود نداره، رد شد.")
                continue
            await conn.execute(text(f"DROP INDEX {index_name}"))
            print(f"🗑  {index_name} روی {table} پاک شد.")
            dropped += 1

    print(f"\n✅ تموم شد. {dropped} ایندکس تکراری پاک شد.")


if __name__ == "__main__":
    asyncio.run(main())
