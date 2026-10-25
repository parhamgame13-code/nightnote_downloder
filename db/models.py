from datetime import datetime
from enum import Enum as PyEnum
from sqlalchemy import (
    Column, Integer, BigInteger, String, Float, Boolean, DateTime, 
    ForeignKey, Enum, Text, Date, Index
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class DownloadStatus(str, PyEnum):
    """وضعیت دانلود"""
    PENDING = "pending"
    DETECTING = "detecting"
    DOWNLOADING = "downloading"
    UPLOADING = "uploading"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FileType(str, PyEnum):
    """نوع فایل"""
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"
    PHOTO = "photo"
    VIDEO_NOTE = "video_note"  # پیام ویدیویی گرد


class User(Base):
    """مدل کاربر"""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    # BigInteger چون آی‌دی کاربرهای جدید تلگرام از سقف Integer معمولی (۲٬۱۴۷٬۴۸۳٬۶۴۷) رد می‌شه
    telegram_id = Column(BigInteger, unique=True, index=True, nullable=False)
    username = Column(String(255), nullable=True, index=True)
    full_name = Column(String(255), nullable=False)
    
    is_banned = Column(Boolean, default=False, index=True)
    ban_reason = Column(Text, nullable=True)
    
    daily_download_count = Column(Integer, default=0)
    last_download_date = Column(Date, nullable=True)
    
    is_admin = Column(Boolean, default=False)

    # تنظیمات شخصی کاربر (منوی /settings)
    custom_thumbnail_file_id = Column(String(255), nullable=True)  # file_id عکسی که کاربر برای thumbnail فایل‌ها فرستاده
    spoiler_effect = Column(Boolean, default=False)  # ویدیو/عکس با افکت اسپویلر (بلور) ارسال بشه
    upload_as_document = Column(Boolean, default=False)  # همیشه به‌جای پلیر این‌لاین، به‌صورت فایل/سند بفرست
    timezone = Column(String(64), nullable=True)  # مثلاً Asia/Tehran - برای نمایش زمان‌ها به وقت محلی کاربر
    show_rename_option = Column(Boolean, default=False)  # قبل از دانلود بپرسیم کاربر می‌خواد اسم فایل رو خودش انتخاب کنه یا نه
    receive_screenshots = Column(Boolean, nullable=True)  # NULL/True یعنی روشن (پیش‌فرض)؛ False یعنی کاربر خاموشش کرده

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # روابط
    downloads = relationship("DownloadJob", back_populates="user", cascade="all, delete-orphan")

    __table_args__ = ()


class DownloadJob(Base):
    """مدل کار دانلود"""
    __tablename__ = "download_jobs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # BigInteger چون آی‌دی سوپرگروپ‌ها تو تلگرام معمولاً منفیِ ۱۳ رقمیه (مثلاً -1001234567890)
    # که خیلی بیشتر از سقف Integer معمولیه؛ با Integer معمولی روی Postgres با خطای
    # "integer out of range" کرش می‌کرد و کل جاب (و در نتیجه ری‌اکشن/ارسال فایل) هیچ‌وقت ساخته نمی‌شد
    chat_id = Column(BigInteger, nullable=False, index=True)  # چت یا گروپ که دانلود درخواست شد
    message_id = Column(Integer, nullable=True)  # پیام اصلی
    status_message_id = Column(Integer, nullable=True)  # پیام «در حال دانلود» بات که بعد از اتمام کار (موفق یا ناموفق) پاک می‌شه تا چت شلوغ نشه
    
    url = Column(String(2048), nullable=False, index=True)
    detected_site = Column(String(100), nullable=False)  # youtube, spotify, soundcloud, ...
    raw_domain = Column(String(255), nullable=True, index=True)  # netloc واقعی URL (مثلاً "cdn123.example.com")
    # صرف‌نظر از این‌که detected_site چی تشخیص داده بشه؛ برای آمار ادمین
    # (خصوصاً پیداکردن دامنه‌هایی که مدام "unknown" می‌شن) نگه‌داشته می‌شه
    
    file_type = Column(Enum(FileType), default=FileType.VIDEO)
    
    # متادیتا
    title = Column(String(500), nullable=True)
    artist = Column(String(255), nullable=True)
    album = Column(String(255), nullable=True)
    duration = Column(Integer, nullable=True)  # ثانیه
    thumbnail_url = Column(String(2048), nullable=True)
    description = Column(Text, nullable=True)  # کپشن/توضیحات کامل پست (قبلاً فقط title ذخیره می‌شد که خلاصه‌ست)
    
    # کیفیت و تنظیمات
    selected_quality = Column(String(50), nullable=True)  # best, 1080p, 720p, audio, ...
    want_direct_link = Column(Boolean, default=False)  # کاربر علاوه بر آپلود تلگرام، لینک دانلود مستقیم هم خواسته
    direct_link_filename = Column(String(255), nullable=True)  # اسم انگلیسیِ دلخواه کاربر برای لینک مستقیم (اختیاری)

    # لینک مستقیم - توکن/URL/انقضا/آمار (برای تمدید دستی و شمارش بازدید؛
    # قبلاً این‌ها فقط تو حافظه‌ی همون تسک celery بودن و بعدش گم می‌شدن)
    direct_link_token = Column(String(64), nullable=True, unique=True, index=True)
    direct_link_url = Column(String(2048), nullable=True)
    direct_link_expires_at = Column(DateTime, nullable=True)
    direct_link_hit_count = Column(Integer, default=0)
    
    # وضعیت
    status = Column(Enum(DownloadStatus), default=DownloadStatus.PENDING, index=True)
    
    # فایل
    file_name = Column(String(500), nullable=True)
    file_size_mb = Column(Float, nullable=True)
    # بدون unique: file_id تلگرام برای یه فایل ثابته و دو job مختلف که همزمان
    # همون مدیا رو می‌فرستن file_id یکسان می‌گیرن؛ با UNIQUE، آپدیتِ DONE دومی
    # IntegrityError می‌خورد و job دوم با وجود ارسال موفق، FAILED ثبت می‌شد.
    # (دیتابیس‌های قدیمی: migration 008 این constraint رو برمی‌داره)
    telegram_file_id = Column(String(255), nullable=True)
    
    # خطا
    error_msg = Column(Text, nullable=True)
    
    # Celery
    celery_task_id = Column(String(255), nullable=True, unique=True, index=True)
    
    # پلی‌لیست
    playlist_id = Column(String(255), nullable=True, index=True)
    playlist_index = Column(Integer, nullable=True)
    playlist_total = Column(Integer, nullable=True)
    
    # زمان‌ها
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    # روابط
    user = relationship("User", back_populates="downloads")

    __table_args__ = (
        Index("idx_job_user_created", "user_id", "created_at"),
        Index("idx_job_status_created", "status", "created_at"),
        Index("idx_job_playlist", "playlist_id"),
    )


class FileCache(Base):
    """کش فایل‌ها برای جلوگیری از دانلود تکراری"""
    __tablename__ = "file_cache"

    id = Column(Integer, primary_key=True, index=True)
    
    # کلید کش (hash از url + quality)
    url_hash = Column(String(255), unique=True, nullable=False, index=True)
    source_url = Column(String(2048), nullable=False)
    quality = Column(String(100), nullable=False)
    
    # تلگرام
    telegram_file_id = Column(String(255), nullable=False)
    file_type = Column(Enum(FileType), nullable=False)
    
    # معلومات فایل
    file_name = Column(String(500), nullable=False)
    file_size_mb = Column(Float, nullable=True)
    
    # متادیتا (برای موسیقی)
    title = Column(String(500), nullable=True)
    artist = Column(String(255), nullable=True)
    album = Column(String(255), nullable=True)
    duration = Column(Integer, nullable=True)
    description = Column(Text, nullable=True)  # کپشن/توضیحات کامل پست
    
    # آمار
    hit_count = Column(Integer, default=0)
    
    # زمان‌ها
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    last_used_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = ()


class BotConfig(Base):
    """تنظیمات بات (دینامیکی)"""
    __tablename__ = "bot_config"

    id = Column(Integer, primary_key=True, index=True)
    
    key = Column(String(255), unique=True, nullable=False, index=True)
    value = Column(Text, nullable=False)
    
    label_fa = Column(String(255), nullable=True)
    description = Column(Text, nullable=True)
    value_type = Column(String(50), default="str")  # str, int, bool, list
    
    updated_by = Column(Integer, nullable=True)  # telegram_id ادمین
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = ()


class DownloadStats(Base):
    """آمار دانلود‌ها"""
    __tablename__ = "download_stats"

    id = Column(Integer, primary_key=True, index=True)
    
    date = Column(Date, unique=True, nullable=False, index=True)
    
    total_downloads = Column(Integer, default=0)
    successful_downloads = Column(Integer, default=0)
    failed_downloads = Column(Integer, default=0)
    
    total_size_mb = Column(Float, default=0)
    total_users = Column(Integer, default=0)
    
    most_used_site = Column(String(100), nullable=True)
    
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
