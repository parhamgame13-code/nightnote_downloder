"""
تنظیمات Celery و Queue
"""
from urllib.parse import urlsplit, urlunsplit

from celery import Celery
from kombu import Queue, Exchange

from bot.config import settings
from utils.logger import setup_logger

# این‌جا صدا زده می‌شه چون worker/beat هر کدوم پروسه‌ی پایتونِ جدا و مستقلن
# (نه زیرپردازه‌ی bot/main.py) - بدون این خط، لاگ‌های worker (مثلاً وقتی
# دانلود توییتر/پینترست/... fail می‌شه) هیچ‌وقت تو logs/bot_*.log یا
# logs/errors_*.log ذخیره نمی‌شدن.
setup_logger()


def _redis_url_with_db(base_url: str, db: int) -> str:
    """آدرس ردیس رو با شماره‌ی دیتابیس مشخص برمی‌گردونه، صرف‌نظر از این‌که
    base_url از قبل شماره‌ی دیتابیس داشته باشه یا نه (جایگزین می‌کنه، اضافه نمی‌کنه)"""
    parts = urlsplit(base_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{db}", parts.query, parts.fragment))


# ایجاد Celery app
celery_app = Celery(
    "downloader_bot",
    broker=settings.CELERY_BROKER_URL or settings.REDIS_URL,
    backend=settings.CELERY_RESULT_BACKEND or _redis_url_with_db(settings.REDIS_URL, settings.REDIS_DB_JOBS),
)

# تنظیمات
celery_app.conf.update(
    # تنظیمات صف
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    
    # تایم‌اوت‌ها
    task_soft_time_limit=1800,  # 30 دقیقه
    task_time_limit=3600,  # 1 ساعت

    # retry خودکار Celery حذف شد (قبلاً: task_autoretry_for=(Exception,) +
    # task_max_retries=3 + task_default_retry_delay=60): autoretry سراسری روی
    # Exception بود، یعنی حتی یه خطای گذرای دیتابیس («database is locked»)
    # وسط یک دانلودِ نیمه‌کاره کل تسک رو از نو اجرا می‌کرد - نتیجه‌ش فایل و
    # پیام تکراری برای کاربر بود. مسیر دانلود خطاها رو خودش هندل و به کاربر
    # گزارش می‌کنه؛ retry سطح Celery برای این تسک‌ها بیشتر ضرر داره تا فایده.

    # Worker
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=1000,

    # Result backend
    result_expires=3600,

    # زمان‌بندی خودکار (Celery Beat) - بدون این، cleanup_temp_files هرگز اجرا
    # نمی‌شد و فایل‌های مؤقت روی هارد سرور جمع می‌شدن تا پر بشه
    beat_schedule={
        'cleanup-temp-files-hourly': {
            'task': 'worker.tasks.cleanup_temp_files',
            'schedule': 3600.0,  # هر یک ساعت
        },
        'check-disk-space-every-15m': {
            'task': 'worker.tasks.check_disk_space',
            'schedule': 900.0,  # هر ۱۵ دقیقه - هشدار ادمین + بستن موقت دانلودها
        },
        'cleanup-old-jobs-daily': {
            'task': 'worker.tasks.cleanup_old_jobs',
            'schedule': 86400.0,  # روزانه - حذف تاریخچه‌ی قدیمی (JOB_RETENTION_DAYS)
        },
        'update-ytdlp-every-6h': {
            'task': 'worker.tasks.update_ytdlp',
            'schedule': 6 * 3600.0,  # هر ۶ ساعت — ساندکلود/اینستاگرام مدام تغییر می‌کنن
        },
        'check-instagram-cookie-every-2h': {
            'task': 'worker.tasks.check_instagram_cookie',
            'schedule': 2 * 3600.0,  # هر ۲ ساعت - کوکی حتی چند روزه هم می‌تونه بمیره
        },
    },
)

# تعریف صف‌ها - صف 'detect' حذف شد: تعریفشده بود ولی هیچ تسکی بهش route
# نمی‌شد (taskهای واقعی همه با queue='download'/'upload'/'default' روی خود
# دکوراتورشون ثبت می‌شن)؛ consuming اضافه‌ی worker هم بی‌اثر بود.
celery_app.conf.task_queues = (
    Queue(
        'download',
        Exchange('download', type='direct'),
        routing_key='download'
    ),
    Queue(
        'upload',
        Exchange('upload', type='direct'),
        routing_key='upload'
    ),
    Queue(
        'default',
        Exchange('default', type='direct'),
        routing_key='default'
    ),
)

# توجه: مسیریابی صف واقعی از طریق queue=... که مستقیم روی هر @celery_app.task
# تو worker/tasks.py گذاشته شده انجام می‌شه (نه از این‌جا). قبلاً اینجا یه
# task_routes با الگوی 'worker.tasks.detect.*' بود که چون اسم واقعی تسک‌ها
# (detect_formats, download_file, send_to_telegram) نقطه بعد از detect/download/
# upload نداره، هیچ‌وقت match نمی‌شد و صرفاً کد مرده و گمراه‌کننده بود؛ حذفش کردیم.
# (یه بلاک conf.worker_queues با فرمت دیکشنری هم اینجا بود که اصلاً تنظیم
# شناخته‌شده‌ی Celery نیست و بی‌صدا نادیده گرفته می‌شد؛ اون هم حذف شد.)
