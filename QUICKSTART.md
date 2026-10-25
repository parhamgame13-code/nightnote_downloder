# 🚀 شروع سریع

## ۱️⃣ نصب (۵ دقیقه)

```bash
# Clone
git clone <repo>
cd downloader_bot

# محیط مجازی
python -m venv venv
source venv/bin/activate

# وابستگی‌ها
pip install -r requirements.txt

# تنظیمات
cp .env.example .env
# .env را ویرایش کنید:
# - BOT_TOKEN خود را وارد کنید
# - ADMIN_IDS شامل شناسه تلگرام خود
```

## ۲️⃣ اجرای کامل با یک دستور

```bash
bash run.sh
```

این یک دستور همه‌چیز رو بالا میاره: Redis، Local Bot API Server (با Docker)، Celery worker، Celery beat، و خود ربات. برای توقف کامل کافیه Ctrl+C بزنی.

اولین بار قبلش مطمئن شو Docker نصبه (`docker --version`) و فایل `.env` رو پر کردی.

## ۳️⃣ اجرای دستی (اختیاری، اگه خواستی هرکدوم رو جدا کنترل کنی)

```bash
# ترمینال 1
docker compose up -d redis telegram-bot-api

# ترمینال 2
python -m bot.main

# ترمینال 3
celery -A worker.celery_app worker --loglevel=info -Q download,upload,default

# ترمینال 4 (اختیاری)
celery -A worker.celery_app beat --loglevel=info
```

## 🔑 متغیرهای مهم .env

```env
BOT_TOKEN=7....:AAAA...              # از BotFather
ADMIN_IDS=123456789                  # شناسه تلگرام شما
REDIS_URL=redis://localhost:6379/0   # برای Celery
MAX_FILE_SIZE_MB=2000                 # حداکثر سایز (با Local Bot API Server تا ۲ گیگ)
USE_LOCAL_BOT_API_SERVER=true         # اتصال به Local Bot API Server به‌جای api.telegram.org
LOCAL_BOT_API_URL=http://localhost:8081
TEMP_DIR=/tmp/downloader_bot          # پوشه موقت
```

## ✅ بررسی

```bash
# بات خوب کار می‌کند؟
curl https://api.telegram.org/botTOKEN/getMe

# Redis؟
redis-cli ping
# نتیجه: PONG

# دیتابیس؟
ls -la bot.db
```

## 🆘 مشاکل شایع

### خطا: `ModuleNotFoundError`
```bash
source venv/bin/activate  # محیط را فعال کنید
```

### خطا: `BOT_TOKEN` خالی
```bash
nano .env  # مقدار صحیح را وارد کنید
```

### Redis اتصال ندارد
```bash
redis-cli  # یا
docker run -d -p 6379:6379 redis:latest
```

### دیتابیس خراب شد
```bash
rm bot.db
python -m bot.main  # دوباره ایجاد می‌شود
```

## 📚 درباره‌تر کارها

- [README.md](README.md) - توضیحات کامل
- [db/models.py](db/models.py) - ساختار دیتابیس
- [bot/main.py](bot/main.py) - کد بات
- [worker/tasks.py](worker/tasks.py) - کارهای صف

## 🎯 بعد از راه‌اندازی

1. ✅ بات را start کنید: `/start`
2. ✅ یک لینک بفرستید: `https://www.youtube.com/watch?v=...`
3. ✅ صبر کنید تا دانلود شود
4. ✅ فایل دریافت کنید
5. ✅ برای تنظیمات شخصی (thumbnail، اسپویلر، رنیم و غیره): `/settings`
6. ✅ برای دیتابیس: `alembic upgrade head` (بعد از هر آپدیت کد، یه بار بزن)

## 📝 نکات

- 🔴 **Redis الزامی است** برای Celery
- 🟢 **SQLite برای تست**, PostgreSQL برای پروداکشن
- 🟡 **yt-dlp** باید نصب باشد (`pip install yt-dlp`)
- 🔵 **Python 3.11+** لازم است

---

سوالی دارید؟ README.md را بخوانید یا GitHub issues را بررسی کنید!
