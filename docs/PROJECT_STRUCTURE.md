# 📁 ساختار پروژه

## درختی کامل

```
downloader_bot/
│
├── 🤖 bot/                          # بخش بات تلگرام
│   ├── __init__.py
│   ├── main.py                      # نقطه ورود اصلی - handlers، /settings، پنل ادمین
│   ├── config.py                    # تنظیمات از .env
│   └── premium_emoji_middleware.py  # میدل‌ور ایموجی پریمیوم
│
├── 💾 db/                           # دیتابیس
│   ├── __init__.py
│   ├── models.py                    # مدل‌های SQLAlchemy
│   ├── session.py                   # async session factory
│   └── crud.py                      # عملیات CRUD
│
├── 🛠️ worker/                       # Celery و صف‌ها
│   ├── __init__.py
│   ├── celery_app.py                # تنظیمات Celery (beat schedule هم اینجاست)
│   ├── tasks.py                     # tasks (detect, download, upload, cleanup)
│   └── concurrency.py                # محدودیت هم‌زمانی دانلود (Redis semaphore)
│
├── 🔧 utils/                        # ابزارهای کمکی
│   ├── __init__.py
│   ├── link_detector.py             # تشخیص سایت و نوع لینک
│   ├── metadata.py                  # استخراج metadata
│   ├── cache_key.py                 # تابع ساخت cache key
│   ├── apk_downloader.py            # ریزالو کردن لینک مستقیم APK از APKPure
│   ├── error_classifier.py          # ترجمه‌ی خطاهای yt-dlp/httpx به فارسی قابل‌فهم
│   ├── direct_links.py              # سرور لینک مستقیم + sanitize نام فایل
│   ├── instagram_downloader.py      # اکسترکتور اختصاصی اینستاگرام
│   ├── twitter_downloader.py        # اکسترکتور اختصاصی توییتر/X
│   ├── tiktok_downloader.py         # فال‌بک tikwm.com برای تیک‌تاک
│   ├── spotify_downloader.py        # متادیتا/پلی‌لیست اسپاتیفای
│   ├── persian.py                   # ابزار متن فارسی
│   ├── logger.py                    # تنظیمات loguru
│   └── premium_emoji.py             # نگاشت ایموجی پریمیوم
│
├── 🗂️ migrations/                   # Alembic (فعال و پیکربندی‌شده)
│   ├── env.py
│   └── versions/                    # 001 تا 007 (آخرین: تنظیمات کاربر + raw_domain)
│
├── 🧪 tests/                         # تست‌ها (pytest)
├── ⚙️ pytest.ini                     # تنظیمات pytest
├── ⚙️ ruff.toml                      # تنظیمات linter
├── ⚙️ alembic.ini                    # تنظیمات migration (script_location=migrations)
├── 📂 logs/                          # فایل‌های لاگ (git-ignored)
│   └── bot.log
│
├── 📝 README.md                     # راهنمای اصلی (ریشه - کنوانسیون گیت‌هاب)
├── ⚙️ .env.example                  # نمونه متغیرهای محیطی
├── 📦 requirements.txt               # وابستگی‌های Python
├── 📦 requirements-dev.txt           # وابستگی‌های توسعه (pytest)
├── 🐋 Dockerfile                    # ایمیج خودِ بات
├── 🐳 docker-compose.yml            # redis + postgres + bot + worker + beat + telegram-bot-api
│
├── 📁 deploy/                       # 🚀 همه‌چیز مربوط به استقرار
│   ├── README.md                    #   راهنمای اسکریپت‌های deploy
│   ├── deploy.sh                    #   دیپلوی کامل تعاملی (دانلود + نصب + .env)
│   ├── setup_downloader_bot.sh      #   نصب/تعمیر idempotent (بدون دانلود)
│   └── systemd/
│       ├── downloader-bot.service            #   بات + worker + beat
│       └── downloader-bot-directlink.service #   وب‌سرور لینک مستقیم
│
└── 📁 docs/                         # 📚 همه‌ی مستندات
    ├── INDEX.md                     #   شاخص پروژه
    ├── QUICKSTART.md                #   شروع سریع
    ├── DEPLOYMENT.md                #   راهنمای استقرار
    ├── PROJECT_STRUCTURE.md         #   این فایل
    ├── COMPLETE_SUMMARY.md          #   خلاصه کامل
    ├── CHANGELOG.md                 #   تاریخچه‌ی نسخه‌ها
    └── replit.md                    #   راهنمای اجرا روی Replit

```

✅ `alembic` فعال و پیکربندی‌شده‌ست (`alembic.ini` + `migrations/env.py` + `migrations/versions/001` تا `007`)؛
تغییرات دیتابیس با `alembic upgrade head` اعمال می‌شن.

## توضیح هر بخش

### 🤖 bot/
دستیار تلگرام و هندلرها

| فایل | توضیح |
|------|-------|
| `main.py` | شروع بات، polling، handlers |
| `config.py` | settings از .env |

**سطح بعد (برای توسعه):**
```
bot/
├── handlers/        # کمانددها و callback‌ها
│   ├── start.py
│   ├── download.py
│   ├── search.py
│   └── admin.py
├── keyboards/       # دکمه‌ها
│   ├── main_kb.py
│   └── quality_kb.py
├── middlewares/     # middleware‌ها
│   ├── throttle.py
│   └── user_register.py
└── states/          # FSM states
```

### 💾 db/
دیتابیس و عملیات

| فایل | توضیح |
|------|-------|
| `models.py` | User, DownloadJob, FileCache, BotConfig |
| `session.py` | AsyncSession factory |
| `crud.py` | UserCRUD, DownloadJobCRUD, ... |

**مدل‌های اصلی:**
- `User` - کاربران
- `DownloadJob` - کارهای دانلود
- `FileCache` - کش فایل‌ها
- `BotConfig` - تنظیمات دینامیک

### 🛠️ worker/
Celery و صف کار

| فایل | توضیح |
|------|-------|
| `celery_app.py` | تنظیمات Celery و Queue |
| `tasks.py` | download_file, send_to_telegram, cleanup_temp_files, update_ytdlp, delete_message_task |

**صف‌ها:**
- `detect` - شناسایی فرمت‌ها
- `download` - دانلود فایل
- `upload` - ارسال به تلگرام

### 🔧 utils/
ابزارهای کمکی

| فایل | توضیح |
|------|-------|
| `link_detector.py` | تشخیص سایت (YouTube, Spotify, Pinterest, APKPure, ...) |
| `metadata.py` | استخراج metadata (عنوان، هنرمند، ...) - بلاکینگ نیست، تو ترد جدا اجرا می‌شه |
| `cache_key.py` | ساخت cache hash |
| `apk_downloader.py` | پیدا کردن لینک مستقیم APK از صفحه‌ی APKPure |

---

## جریان کار

```
پیام (لینک) از کاربر
     ↓
main.py/handle_message()  → LinkDetector.extract_url() اول لینک رو از وسط متن جدا می‌کنه
     ↓
LinkDetector.detect_site()
     ↓
MetadataExtractor.extract_from_url()   [تو ترد جدا + سقف ۵ تا همزمان، بلاک نمی‌کنه]
     ↓
DownloadJobCRUD.create() → DB
     ↓
tasks.download_file()   [Celery, queue=download]
     ↓
tasks.send_to_telegram() [Celery, queue=upload]  → ریپلای به پیام اصلی + ری‌اکشن 👍/👎
     ↓
FileCacheCRUD.create()  → کش
     ↓
_delete_status_message() پیام «در حال دانلود» پاک می‌شه
     ↓
✅ فایل به کاربر ارسال شد
```
ℹ️ نکته: `update_ytdlp` (هر ۶ ساعت) و `cleanup_temp_files` (هر ۱ ساعت) از طریق
Celery Beat خودکار اجرا می‌شن (`worker/celery_app.py` → `beat_schedule`)، نه
با فراخوانی مستقیم از کد بات.

---

## مدل‌های دیتابیس

### User
```python
- telegram_id: int (PK)
- username: str
- full_name: str
- is_banned: bool
- daily_download_count: int
- last_download_date: date
```

### DownloadJob
```python
- url: str
- detected_site: str          # youtube, spotify, pinterest, apkpure, ...
- status: enum               # pending, downloading, done, failed, cancelled
- file_type: enum            # video, audio, document, photo
- message_id: int            # پیام اصلی کاربر (برای ریپلای و ری‌اکشن)
- status_message_id: int     # پیام «در حال دانلود» (برای پاک کردنش بعد از اتمام کار)
- title: str                 # از metadata
- artist: str                # از metadata
- album: str                 # از metadata
- duration: int              # ثانیه
- telegram_file_id: str      # برای ارسال دوباره
```

### FileCache
```python
- url_hash: str (unique)     # sha256 hash
- quality: str
- telegram_file_id: str      # بدون دانلود دوباره
- hit_count: int             # چند بار استفاده شد
```

### BotConfig
```python
- key: str (unique)          # max_daily_downloads, ...
- value: str
- updated_by: int            # ادمین
```

---

## متغیرهای محیطی (.env)

```
# تلگرام
BOT_TOKEN=...
BOT_USERNAME=...
CHANNEL_USERNAME=...
ADMIN_IDS=123,456,789

# دیتابیس
DATABASE_URL=sqlite+aiosqlite:///./bot.db

# Redis
REDIS_URL=redis://localhost:6379/0

# Celery
CELERY_BROKER_URL=...
CELERY_RESULT_BACKEND=...

# فایل‌ها
TEMP_DIR=/tmp/downloader_bot
MAX_FILE_SIZE_MB=2000

# موسیقی
MUSIC_EXTRACT_METADATA=true
MUSIC_TIMEOUT=300

# لیمیت‌ها
MAX_DAILY_DOWNLOADS=50
DOWNLOAD_TIMEOUT=1800
```

---

## نحوه اجرا

### سادگی (بدون Celery)
```bash
python -m bot.main
```

### کامل (با Celery)
```bash
# ترمینال 1
python -m bot.main

# ترمینال 2
celery -A worker.celery_app worker -Q download,upload

# ترمینال 3
celery -A worker.celery_app beat
```

### Redis/Postgres با Docker (اختیاری)
```bash
docker-compose up -d   # فقط Redis و Postgres رو بالا می‌آره
# خودِ بات و ورکر هنوز Dockerize نشدن، دستی با python/celery اجرا می‌شن (بالا رو ببین)
```

---

## الحاقی‌های مستقبل

⚠️ **قبل از انجامش بخون:** این تقسیم‌بندی (`handlers/`, `keyboards/`, `middlewares/`,
`states/`, `decorators/`) یه‌بار قبلاً واقعاً پیاده‌سازی شده بود ولی هیچ‌وقت به
`bot/main.py` وصل نشد و کاملاً کد مرده و گمراه‌کننده موند (تشخیص داده و حذف شد).
اگه دوباره این مسیر رو رفتی، حتماً مطمئن شو `bot/main.py` واقعاً ازش import
می‌کنه، وگرنه دوباره همون مشکل تکرار می‌شه.

### برای بهتری:
```
bot/
├── handlers/        # تقسیم به چند فایل
├── keyboards/       # کیبورد‌های اضافی
├── middlewares/     # middleware‌های بیشتر
├── states/          # FSM states
└── decorators/      # decorator‌های شخصی

worker/
├── tasks/          # تقسیم tasks به فایل‌های جداگانه
└── schedules/      # Celery Beat schedules
```

### ویژگی‌های آینده:
- [ ] تنظیمات کاربر شخصی
- [ ] پشتیبانی پلی‌لیست
- [ ] جستجو به نام
- [ ] تاریخچه‌ی دانلود
- [ ] Panel ادمین کامل
- [ ] آمار و تحلیل

---

## نکات مهم

✅ **Async/Await** - تمام operations async است
✅ **SQLAlchemy 2.x** - ORM مدرن
⚠️ **Alembic** - تو requirements.txt هست ولی هنوز پیکربندی نشده؛ migration فعلاً دستیه
✅ **Celery** - برای tasks موازی (queue های detect/download/upload)
✅ **Redis** - برای cache و queue
✅ **loguru** - logging خوب
✅ **yt-dlp** - دانلود 1800+ سایت + اسکرپر جدا برای APKPure

---

سوالی دارید؟ README.md یا DEPLOYMENT.md را بخوانید!
