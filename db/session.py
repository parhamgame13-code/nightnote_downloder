"""
تنظیمات دیتابیس و session factory
"""
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from bot.config import settings


# ایجاد engine
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    connect_args={
        "timeout": 30,
        "check_same_thread": False,
    } if "sqlite" in settings.DATABASE_URL else {},
)

if "sqlite" in settings.DATABASE_URL:
    # با چند هزار کاربر، هم پروسه‌ی بات و هم چندتا celery worker همزمان دارن
    # رو یه فایل SQLite می‌نویسن. بدون WAL، نوشتن قفل‌کننده‌ی کل فایله و به
    # محض این‌که دو تا نوشتن همزمان بشه، خطای "database is locked" می‌گیریم
    # که تسک‌ها رو fail می‌کنه. WAL خوندن/نوشتن همزمان رو ممکن می‌کنه؛
    # busy_timeout هم به‌جای کرش فوری، چند ثانیه صبر می‌کنه تا قفل باز بشه.
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

# session factory
async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def get_session() -> AsyncSession:
    """دریافت session جدید"""
    async with async_session_maker() as session:
        yield session


async def init_db():
    """مقداردهی دیتابیس"""
    from db.models import Base
    
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # مقداردهی اولیه‌ی تنظیمات پیش‌فرض (فقط کلیدهایی که هنوز تو دیتابیس نیستن رو
    # اضافه می‌کنه، پس صدا زدنش رو هر بار استارت بی‌خطره)
    from db.seed import seed_default_config
    await seed_default_config()


async def close_db():
    """بستن اتصال دیتابیس"""
    await engine.dispose()
