from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, NoDecode
from typing import Optional, Annotated


class Settings(BaseSettings):
    """تنظیمات بات از .env"""
    model_config = SettingsConfigDict(
        env_file=".env", 
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # تلگرام
    BOT_TOKEN: str
    BOT_USERNAME: str = "NightDownloaderBot"
    
    # کانال‌ها/گروپ‌ها
    CHANNEL_USERNAME: str = "NightNote_official"
    ALLOWED_GROUP_IDS: Annotated[list[int], NoDecode] = []  # "123,456" در .env
    
    # ادمین
    ADMIN_IDS: Annotated[list[int], NoDecode] = []  # "123,456" در .env
    
    # اینستاگرام
    INSTAGRAM_COOKIE_FILE: Optional[str] = None
    # یوتیوب - اختیاری. IP سرورهای دیتاسنتری این‌روزها بعضی‌وقتا از سمت
    # یوتیوب با 403 رد می‌شن (حتی برای ویدیوهای کاملاً عمومی)؛ کوکی یه
    # اکانت لاگین‌شده (فایل Netscape-format، مثل INSTAGRAM_COOKIE_FILE) اگه
    # ست بشه، به‌عنوان یکی از تلاش‌های fallback بعد از 403 استفاده می‌شه.
    # اختیاریه - اگه ست نشه، فقط با player_client جایگزین (بدون کوکی) تلاش می‌شه.
    YOUTUBE_COOKIE_FILE: Optional[str] = None

    # FileLu - کلید API اکانت (اختیاری). بدون این، لینک‌های filelu.com پشتیبانی
    # نمی‌شن چون دانلود مستقیم رایگان از خودِ سایت مسدوده و فقط از طریق API
    # اکانت (پولی/premium) قابل دسترسیه.
    FILELU_API_KEY: Optional[str] = None
    
    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_DB_JOBS: int = 2
    
    # دیتابیس
    DATABASE_URL: str = "sqlite+aiosqlite:///./bot.db"
    # DATABASE_URL: str = "postgresql+asyncpg://user:pass@localhost/downloader_bot"
    
    # Celery
    CELERY_BROKER_URL: Optional[str] = None  # اگه None باشه از REDIS_URL استفاده میشه
    CELERY_RESULT_BACKEND: Optional[str] = None
    
    # فایل‌ها
    TEMP_DIR: str = "/tmp/downloader_bot"
    MAX_FILE_SIZE_MB: int = 2000  # سقف Local Bot API Server (تا ۲ گیگ)
    MAX_WARN_SIZE_MB: int = 700  # بالاتر از این حجم برای لینک‌های فایل مستقیم، قبل از دانلود از کاربر تاییدیه می‌گیریم
    USE_LOCAL_BOT_API_SERVER: bool = True
    LOCAL_BOT_API_URL: str = "http://localhost:8081"
    # aiogram's AiohttpSession defaults to 60s؛ برای فایل‌های حجیم
    # (مثلاً ویدیوی ۱.۵ گیگ) آپلود راحت بیشتر از این طول می‌کشه و
    # کانکشن قبل از تمومِ آپلود timeout می‌خوره. این مقدار رو بزرگ‌تر
    # می‌کنیم (پیش‌فرض ۳۰ دقیقه) تا برای فایل‌های بزرگ هم کار کنه.
    BOT_SESSION_TIMEOUT: int = 1800  # 30 دقیقه

    # پروکسی اختیاری برای اتصال بات به Telegram API (خالی = مستقیم).
    # نکته: aiohttp (برخلاف curl/yt-dlp) متغیرهای پروکسی محیط رو نادیده
    # می‌گیره؛ build_bot_session اگه این ست نباشه خودش HTTPS_PROXY/HTTP_PROXY
    # محیط رو هم چک می‌کنه و به سشن پاس می‌ده.
    TELEGRAM_PROXY: Optional[str] = None

    # Local Bot API Server خودش هر فایلی که آپلود/دانلود می‌شه رو زیر
    # /var/lib/telegram-bot-api/<token>/... برای همیشه نگه می‌داره (خودش
    # هیچ‌وقت پاکش نمی‌کنه) - این باعث می‌شه هر فایل عملاً دوبار رو دیسک
    # باشه (یه بار اینجا، یه بار تو DIRECT_LINK_DIR/TEMP_DIR). پاک‌سازیش
    # مسئولیت خودمونه، تو cleanup_temp_files انجام می‌شه.
    BOT_API_LOCAL_FILES_DIR: str = "/var/lib/telegram-bot-api"
    BOT_API_LOCAL_FILES_MAX_AGE_HOURS: int = 2

    # بعضی فایل‌های فوروارد شده‌ی حجیم (۱-۲ گیگ) رو Local Bot API Server
    # موقع صدا زدن get_file باید اول از سرورهای تلگرام بگیره و رو دیسک خودش
    # بنویسه - این مرحله قبل از این‌که ما اصلاً شروع کنیم به کپی/گزارش
    # پیشرفت اتفاق می‌افته، پس نوار وضعیت طی این مدت رو صفر ثابت می‌مونه.
    # اگه این مرحله بیش از حد طول بکشه (شبکه/سرور تلگرام کند باشه)، به‌جای
    # این‌که بذاریم Celery بعد از یک ساعت (task_time_limit) با SIGKILL
    # بکشتش (که یعنی هیچ کد پاک‌سازی/پیام خطایی اجرا نمی‌شه)، خودمون با یه
    # timeout مشخص و کوتاه‌تر، تمیز failure می‌دیم.
    TELEGRAM_GET_FILE_TIMEOUT_SECONDS: int = 1200
    
    # لینک دانلود مستقیم (علاوه بر آپلود در تلگرام)
    DIRECT_LINK_ENABLED: bool = False
    # آدرس عمومی که فایل‌ها ازش سرو می‌شن، بدون / آخر (مثلاً https://dl.nightnote.ir یا http://1.2.3.4:8090)
    DIRECT_LINK_PUBLIC_URL: str = ""
    # پوشه‌ای که کپی/هاردلینک فایل‌های قابل‌دانلود مستقیم توش نگه‌داری می‌شه
    DIRECT_LINK_DIR: str = "/var/lib/downloader_bot/direct_links"
    # پورتی که وب‌سرور اختصاصی لینک مستقیم (webserver/direct_link_server.py) روش بالا میاد
    DIRECT_LINK_PORT: int = 8090
    # بعد از چند ثانیه لینک/فایل مستقیم خودکار پاک بشه (پیش‌فرض ۶ ساعت)
    DIRECT_LINK_EXPIRE_SECONDS: int = 14400

    # موسیقی - حذف شد: MUSIC_EXTRACT_METADATA/MUSIC_FALLBACK_TITLE_ON_EMPTY/
    # MUSIC_TIMEOUT هیچ‌جا خونده نمی‌شدن (متادیتای موسیقی همیشه از yt-dlp/
    # اسکرپرها میاد).

    # لیمیت‌ها
    MAX_DAILY_DOWNLOADS: int = 50  # سقف روزانه‌ی «دانلود از لینک» (سایت‌های خارجی)
    # سقف روزانه‌ی جداگانه‌ی «آپلود مستقیم» (کاربر خودش فایل می‌فرسته، فقط لینک
    # می‌گیره) - چون این مسیر پهنای‌باند دانلود خارجی مصرف نمی‌کنه (فقط دیسک/کپی
    # محلی)، منطقی نیست از همون سهمیه‌ی MAX_DAILY_DOWNLOADS کم بشه.
    MAX_DAILY_DIRECT_UPLOADS: int = 50
    MAX_CONCURRENT_DOWNLOADS: int = 100  # سقف واقعی دانلود همزمان (اجرا می‌شه، نه فقط تنظیمات مرده)
    DOWNLOAD_TIMEOUT: int = 1800  # 30 دقیقه
    
    # لاگینگ
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = "logs/bot.log"

    # نگهبان فضای دیسک (تسک دوره‌ای check_disk_space): آستانه‌ها به درصدِ
    # پر بودن دیسکِ میزبانِ TEMP_DIR/DIRECT_LINK_DIR. بالای WARN به ادمین‌ها
    # هشدار می‌ره؛ بالای CRITICAL دانلودهای جدید موقتاً بسته می‌شه (چک در
    # bot/main.py)؛ برگشت به عادی با هیسترزیس (RESUME) که روی مرز نلرزه.
    DISK_WARN_PERCENT: int = 85
    DISK_CRITICAL_PERCENT: int = 95
    DISK_RESUME_PERCENT: int = 80

    # نگه‌داری تاریخچه‌ی jobها (روز) - تسک دوره‌ای cleanup_old_jobs رکوردهای
    # قدیمی‌تر رو حذف می‌کنه تا download_jobs بی‌نهایت رشد نکنه. ۰ یعنی غیرفعال.
    JOB_RETENTION_DAYS: int = 90

    # آپدیت خودکار yt-dlp/curl_cffi توسط تسک دوره‌ای beat (هر ۶ ساعت).
    # خاموشش کنید اگه می‌خواید نسخه‌ها کاملاً پین بمونن - آپگرید وسط اجرا
    # ممکنه رفتار دانلود رو بدون اطلاع عوض کنه.
    AUTO_UPDATE_YTDLP: bool = True
    
    @field_validator("ADMIN_IDS", "ALLOWED_GROUP_IDS", mode="before")
    @classmethod
    def _parse_id_list(cls, value):
        """تبدیل رشته‌ی جدا شده با کاما (مثل '123,456') به لیست int
        قبل از این‌که pydantic-settings سعی کنه با json.loads پارسش کنه"""
        if isinstance(value, str):
            return [int(x.strip()) for x in value.split(",") if x.strip()]
        return value or []


settings = Settings()


def build_bot_session():
    """یه AiohttpSession می‌سازه با timeout بزرگ‌تر از پیش‌فرض ۶۰ ثانیه‌ی
    aiogram (که برای آپلود فایل‌های حجیم مثل ویدیوی ۱.۵ گیگ کافی نیست).
    اگه USE_LOCAL_BOT_API_SERVER روشن باشه، به Local Bot API Server
    (به‌جای api.telegram.org) هم وصل می‌شه."""
    import os

    from aiogram.client.session.aiohttp import AiohttpSession

    proxy = (
        settings.TELEGRAM_PROXY
        or os.getenv("HTTPS_PROXY")
        or os.getenv("https_proxy")
        or os.getenv("HTTP_PROXY")
        or os.getenv("http_proxy")
    )
    kwargs = {"timeout": settings.BOT_SESSION_TIMEOUT}
    if proxy:
        kwargs["proxy"] = proxy

    if not settings.USE_LOCAL_BOT_API_SERVER:
        return AiohttpSession(**kwargs)

    from aiogram.client.telegram import TelegramAPIServer

    server = TelegramAPIServer.from_base(settings.LOCAL_BOT_API_URL, is_local=True)
    return AiohttpSession(api=server, **kwargs)
