# ربات دانلودر یونیورسال

ربات تلگرامی برای دانلود از ۱۸۰۰+ سایت. پشتیبانی از پیوی و گروه.

## اجرا

```bash
cd downloader_bot
pip install -r requirements.txt
cp .env.example .env
# .env را با BOT_TOKEN و سایر تنظیمات پر کن
alembic upgrade head
python -m bot.main
```

## فیچرهای اصلی
- دانلود از یوتیوب/اینستاگرام/تیک‌تاک/اسپاتیفای/ساندکلاود/... + پلی‌لیست
- `/settings` - تنظیمات شخصی (thumbnail، اسپویلر، رنیم، حالت سند)
- پنل ادمین: `/stats`, `/topsites`, `/userinfo`, `/broadcast`

## User preferences

- پروژه Python — ربات تلگرام با aiogram 3.x
- زبان فارسی برای پیام‌های ربات
- پشتیبانی از گروه تلگرام
- فرمت موزیک: MP3 با متادیتا کامل
