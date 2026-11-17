FROM python:3.11-slim

# نصب FFmpeg و libmagic (برای تبدیل صدا به MP3 و embed cover art)
# + redis-server (برای حالت تک‌کانتینری run_all.py: ردیس توکار وقتی Redis
# جدا در دسترس نیست) و curl (برای healthcheck/دیباگ)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libmagic1 \
    redis-server \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# کپی requirements و نصب
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# کپی کد پروژه
COPY . .

# دایرکتوری موقت و لاگ و داده (دیتابیس SQLite روی PaaS اگه DATABASE_URL
# به /app/data اشاره کنه، با دیسک دائمی قابل نگه‌داشتنه)
RUN mkdir -p /tmp/downloader_bot logs /app/data

# متغیرهای محیطی پیش‌فرض (بازنویسی در docker-compose.yml)
ENV TEMP_DIR=/tmp/downloader_bot
ENV DATABASE_URL=sqlite+aiosqlite:///./bot.db
ENV REDIS_URL=redis://redis:6379/0
ENV PORT=8000

EXPOSE 8000

# اجرای همه‌چیز با هم: وب (روی $PORT) + بات + ورکر + beat
# سرویس‌های docker-compose هر کدوم `command` خودشون رو دارن و این CMD رو
# بازنویسی می‌کنن، پس تغییر CMD رفتار compose رو عوض نمی‌کنه.
CMD ["python", "run_all.py"]
