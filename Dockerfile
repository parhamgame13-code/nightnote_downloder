FROM python:3.11-slim

# نصب FFmpeg و libmagic (برای تبدیل صدا به MP3 و embed cover art)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# کپی requirements و نصب
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# کپی کد پروژه
COPY . .

# دایرکتوری موقت و لاگ
RUN mkdir -p /tmp/downloader_bot logs

# متغیرهای محیطی پیش‌فرض (بازنویسی در docker-compose.yml)
ENV TEMP_DIR=/tmp/downloader_bot
ENV DATABASE_URL=sqlite+aiosqlite:///./bot.db
ENV REDIS_URL=redis://redis:6379/0

# اجرای ربات (Polling — بدون Webhook)
CMD ["python", "-m", "bot.main"]
