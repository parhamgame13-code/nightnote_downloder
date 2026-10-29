import asyncio
import os
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from dotenv import load_dotenv

# مسیر صریح: بدون این، find_dotenv به دایرکتوریِ فراخوان وابسته‌ست و روی
# محیط‌هایی مثل Deepnote (cwd متفاوت / .env در مسیر دیگه) ممکنه فایل اشتباه
# (یا هیچ فایلی) لود شه و DATABASE_URL پیش‌فرض یا کهنه استفاده بشه.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# این مقدار از alembic.ini خونده میشه
config = context.config

# لاگ‌گذاری
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# مدل‌های SQLAlchemy
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.models import Base
target_metadata = Base.metadata


def get_url():
    return os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./bot.db")


def run_migrations_offline() -> None:
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _to_async_url(url: str) -> str:
    """درایور دیتابیس رو به نسخه‌ی async درست تبدیل می‌کنه.

    قبلاً این کار با .replace("sqlite://", "sqlite+aiosqlite://") انجام می‌شد که
    یه باگ جدی داشت: وقتی DATABASE_URL از قبل "sqlite+aiosqlite:///./bot.db" بود
    (یعنی همون مقدار پیش‌فرضه)، چون خودِ "aiosqlite://" در انتهاش دقیقاً زیررشته‌ی
    "sqlite://" رو داره، replace روی همون بخش هم اعمال می‌شد و drivername رو به
    "sqlite+aiosqlite+aiosqlite" خراب می‌کرد. نتیجه دقیقاً همون ارور
    "ValueError: too many values to unpack (expected 2)" تو dialects/__init__.py
    بود، چون drivername بعد از replace("+", ".") سه تیکه می‌شد نه دو تیکه.
    با make_url/set به‌جای دستکاری رشته‌ی خام، این تبدیل idempotent و امنه."""
    from sqlalchemy.engine.url import make_url

    parsed = make_url(url)
    if parsed.drivername == "sqlite":
        parsed = parsed.set(drivername="sqlite+aiosqlite")
    elif parsed.drivername == "postgresql":
        parsed = parsed.set(drivername="postgresql+asyncpg")
    # اگه از قبل درایور async مشخص شده (sqlite+aiosqlite, postgresql+asyncpg و...)
    # دست نمی‌زنیم و همون رو برمی‌گردونیم
    return str(parsed)


async def run_async_migrations() -> None:
    url = _to_async_url(get_url())

    configuration = config.get_section(config.config_ini_section)
    configuration["sqlalchemy.url"] = url

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
