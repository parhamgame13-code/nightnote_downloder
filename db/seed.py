from sqlalchemy import select
from db.models import BotConfig
from db.session import async_session_maker
from loguru import logger

# نکته: قبلاً یه لیست بلند از کلیدها اینجا seed می‌شد (msg_start، msg_help،
# msg_done، max_daily_downloads، force_join_channel و ...) که عملاً legacy
# بودن و هیچ‌جای کد خونده نمی‌شدن:
#   - سیستم پیام‌های قابل‌ویرایشِ فعال تو utils/texts.py هست (کلیدهای text_*)
#   - سقف دانلود روزانه از settings.MAX_DAILY_DOWNLOADS (env) میاد
#   - عضویت اجباری از کلید force_join_channels (که تو bot/main.py مدیریت
#     می‌شه، نه seed) خونده می‌شه
#   - msg_start حتی به /history اشاره می‌کرد که چنین دستوری وجود نداره
# فقط max_file_size_mb واقعاً مصرف می‌شه (/maxsize، چک حجم آپلودی در بات
# و چک حجم بعد از دانلود در ورکر).

DEFAULT_CONFIG = [
    {"key": "max_file_size_mb", "value": "2000", "label_fa": "حداکثر حجم فایل (MB)", "value_type": "int"},
]


async def seed_default_config() -> None:
    async with async_session_maker() as session:
        for item in DEFAULT_CONFIG:
            result = await session.execute(
                select(BotConfig).where(BotConfig.key == item["key"])
            )
            existing = result.scalar_one_or_none()
            if existing is None:
                session.add(BotConfig(**item))
                logger.info(f"Seeded config: {item['key']}")
        await session.commit()
