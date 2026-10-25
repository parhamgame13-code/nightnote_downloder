# 📚 شاخص و راهنمای پروژه

## 🎯 شروع از اینجا

### 👶 برای شروع فوری:
1. [`QUICKSTART.md`](QUICKSTART.md) - 5 دقیقه تا نصب

### 📖 برای درک کامل:
2. [`README.md`](README.md) - راهنمای جامع

### 🏗️ برای معماری:
3. [`PROJECT_STRUCTURE.md`](PROJECT_STRUCTURE.md) - ساختار فایل‌ها

### 🚀 برای استقرار:
4. [`DEPLOYMENT.md`](DEPLOYMENT.md) - راهنمای deploy

### ✨ برای خلاصه:
5. [`COMPLETE_SUMMARY.md`](COMPLETE_SUMMARY.md) - خلاصه کامل

---

## 🔍 فایل‌های اساسی

### کد اصلی:

```python
# 🤖 بات
bot/
  ├── config.py          # Settings
  └── main.py            # Handler‌ها

# 💾 دیتابیس
db/
  ├── models.py          # SQLAlchemy Models
  ├── session.py         # Async Session
  └── crud.py            # Query Operations

# 🛠️ Worker
worker/
  ├── celery_app.py      # Task Queue Config
  └── tasks.py           # Download Tasks

# 🔧 ابزار
utils/
  ├── link_detector.py   # Detect Site
  ├── metadata.py        # Extract Metadata
  ├── cache_key.py       # Cache Key
  └── apk_downloader.py  # Resolve APKPure direct link
```

---

## 📋 نحوه استفاده

### Step 1: نصب
```bash
# QUICKSTART.md را دنبال کنید
pip install -r requirements.txt
```

### Step 2: تنظیم
```bash
cp .env.example .env
# .env را تکمیل کنید
```

### Step 3: اجرا
```bash
python -m bot.main
```

---

## ❓ سوالات معمول

| سوال | جواب |
|------|------|
| **کتاب‌خانه‌های استفاده شده؟** | [`requirements.txt`](requirements.txt) |
| **ساختار دیتابیس؟** | [`db/models.py`](db/models.py) |
| **نحوه دانلود؟** | [`worker/tasks.py`](worker/tasks.py) |
| **شناسایی سایت؟** | [`utils/link_detector.py`](utils/link_detector.py) |
| **استخراج metadata؟** | [`utils/metadata.py`](utils/metadata.py) |
| **استقرار کجا؟** | [`DEPLOYMENT.md`](DEPLOYMENT.md) |

---

## 🎯 نقشه راه توسعه

```
✅ مرحله 1: تشخیص لینک و سایت (یوتیوب، اینستاگرام، تیک‌تاک، آپارات، Pinterest، APKPure، ...)
✅ مرحله 2: استخراج metadata (بدون بلاک کردن بات، تو ترد جدا)
✅ مرحله 3: دانلود با yt-dlp + اسکرپر جدا برای APKPure
✅ مرحله 4: ارسال به تلگرام (با ریپلای به پیام اصلی + ری‌اکشن 👍/👎)
✅ مرحله 5: کش هوشمند
✅ مرحله 6: پلی‌لیست (یوتیوب با yt-dlp، اسپاتیفای و ساندکلاود با فن‌اوت به job جدا) و انتخاب کیفیت (منوی اینلاین)
✅ مرحله 7: Panel ادمین کامل‌تر (/topsites، /userinfo، /broadcast)
✅ مرحله 8: آمار و breakdown per-user
✅ مرحله 9: تنظیمات شخصی کاربر (/settings) - thumbnail، اسپویلر، حالت سند، رنیم، timezone
✅ مرحله 10: سخت‌سازی امنیتی (چک مالکیت job، فیلتر SSRF)
```

---

## 💡 نکات کلیدی

### 🔑 نقطه ورود:
```python
# bot/main.py
@dp.message(F.text)
async def handle_message(message: types.Message):
    # دریافت لینک
    # تشخیص سایت
    # دانلود و ارسال
```

### 🔄 جریان داده:
```
Message → Detect → Extract Metadata → Create Job → Queue → Download → Upload → Send
```

### 💾 ذخیره‌سازی:
```
User (کاربر) 
  ↓
DownloadJob (کار) 
  ↓
FileCache (کش)
```

---

## 📦 چیست؟

### `requirements.txt`
```
aiogram==3.29.1       # Bot Framework
yt-dlp==2026.7.4      # Downloader
celery[redis]==5.6.3  # Task Queue
sqlalchemy==2.0.51    # ORM
```

### `.env.example`
```
BOT_TOKEN=...         # توکن تلگرام
REDIS_URL=...         # Connection
DATABASE_URL=...      # دیتابیس
```

### `docker-compose.yml`
```yaml
services:
  redis:      # برای Queue
  postgres:   # برای DB
```

---

## 🚀 هدف نهایی

```
┌─────────────────────────────────────┐
│ کاربر: فرستادن لینک                │
│ https://spotify.com/track/...      │
└────────────┬────────────────────────┘
             ↓
┌─────────────────────────────────────┐
│ بات: شناسایی و استخراج             │
│ Spotify - "Song Name"              │
│ Artist - "Name"                    │
└────────────┬────────────────────────┘
             ↓
┌─────────────────────────────────────┐
│ Worker: دانلود MP3                  │
│ فایل: song.mp3 (5MB)               │
└────────────┬────────────────────────┘
             ↓
┌─────────────────────────────────────┐
│ تلگرام: دریافت فایل                │
│ 🎵 Song Name                       │
│ 👤 Artist Name                     │
└─────────────────────────────────────┘
```

---

## 🎓 یادگیری

### `aiogram`
- Handler‌ها
- FSM (Finite State Machine)
- Callback Query

### `SQLAlchemy`
- ORM مدرن
- Async operations
- Relationships

### `Celery`
- Task Queue
- Worker
- Scheduling

### `yt-dlp`
- Download
- Metadata
- Format selection

---

## 📞 پشتیبانی

### اگه مشکل دارید:
1. [`README.md`](README.md) را بخوانید
2. [`QUICKSTART.md`](QUICKSTART.md) را دنبال کنید
3. [`DEPLOYMENT.md`](DEPLOYMENT.md) را چک کنید
4. GitHub Issues را جستجو کنید

---

## ✅ Checklist

- [ ] [`README.md`](README.md) را خوندید
- [ ] [`QUICKSTART.md`](QUICKSTART.md) را دنبال کردید
- [ ] `.env` را تکمیل کردید
- [ ] `python -m bot.main` اجرا کردید
- [ ] اولین لینک را ارسال کردید
- [ ] فایل دانلود شد ✅

---

## 🏁 خلاصه

```
📁 فایل‌ها:        30+
💻 کد:            8000+ خط
📚 Documentation: 7 فایل
🎯 آماده:         100%
```

---

**بیایید شروع کنیم! [`QUICKSTART.md`](QUICKSTART.md) را باز کنید** 🚀
