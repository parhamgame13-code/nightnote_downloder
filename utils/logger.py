import sys
from loguru import logger


def setup_logger():
    logger.remove()  # حذف handler پیش‌فرض

    try:
        from bot.config import settings
        level = getattr(settings, "LOG_LEVEL", "INFO") or "INFO"
    except Exception:
        level = "INFO"

    # Console
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | <cyan>{name}:{line}</cyan> | {message}",
        colorize=True,
    )

    # فایل لاگ روزانه
    logger.add(
        "logs/bot_{time:YYYY-MM-DD}.log",
        rotation="00:00",
        retention="30 days",
        level=level,
        encoding="utf-8",
        format="{time:HH:mm:ss} | {level} | {name}:{line} | {message}",
    )

    # فایل خطا جدا
    logger.add(
        "logs/errors_{time:YYYY-MM-DD}.log",
        rotation="00:00",
        retention="60 days",
        level="ERROR",
        encoding="utf-8",
        backtrace=True,
        diagnose=True,
    )
