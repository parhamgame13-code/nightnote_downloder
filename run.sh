#!/bin/bash
# اجرای کامل NightDownloaderBot با یک دستور:
#   Redis + Local Bot API Server (Docker) + Celery worker + Celery beat + ربات
#
# استفاده:
#   bash run.sh

set -e
cd "$(dirname "$0")"

if [ ! -f .env ]; then
    echo "❌ فایل .env پیدا نشد. اول از .env.example کپی کن و پرش کن:"
    echo "   cp .env.example .env"
    exit 1
fi

mkdir -p logs

if [ -d "venv" ]; then
    source venv/bin/activate
fi
sudo mkdir -p /var/lib/telegram-bot-api 2>/dev/null || mkdir -p /var/lib/telegram-bot-api

echo "🧹 بستن نسخه‌های قبلی celery/bot که ممکنه از اجراهای قبلی زنده مونده باشن..."
pkill -9 -f "celery -A worker.celery_app" 2>/dev/null || true
pkill -9 -f "python3 -m bot.main" 2>/dev/null || true
sleep 1

echo "🐳 بالا آوردن Redis..."
docker compose up -d redis

# Local Bot API Server (برای فایل‌های تا ۲ گیگ):
# - اگه از قبل چیزی روی 8081 هست (مثلاً کانتینر مشترک بین چند بات)، از همون استفاده می‌شه
# - وگرنه سرویس telegram-bot-api خود compose بالا میاد (کلیدهای پیش‌فرض عمومی
#   داره؛ با TELEGRAM_API_ID/TELEGRAM_API_HASH تو .env قابل بازنویسیه)
if curl -s -o /dev/null "http://localhost:8081"; then
    echo "✅ یه Local Bot API Server از قبل روی پورت 8081 هست - از همون استفاده می‌شه."
else
    echo "🐳 بالا آوردن Local Bot API Server (telegram-bot-api)..."
    docker compose up -d telegram-bot-api
    echo "🔎 منتظر آماده شدن پورت 8081..."
    ready=0
    for i in $(seq 1 30); do
        if curl -s -o /dev/null "http://localhost:8081"; then
            ready=1
            break
        fi
        sleep 2
    done
    if [ "$ready" != "1" ]; then
        echo "❌ بعد از ۶۰ ثانیه چیزی روی پورت 8081 جواب نداد."
        echo "   لاگش: docker compose logs telegram-bot-api"
        exit 1
    fi
    echo "✅ Local Bot API Server در دسترسه."
fi

echo "🔧 اجرای Celery worker..."
celery -A worker.celery_app worker --loglevel=info -Q download,upload,default \
    > logs/celery_worker.log 2>&1 &
WORKER_PID=$!

echo "⏰ اجرای Celery beat..."
celery -A worker.celery_app beat --loglevel=info \
    > logs/celery_beat.log 2>&1 &
BEAT_PID=$!

cleanup() {
    echo ""
    echo "🛑 در حال متوقف کردن worker و beat..."
    kill "$WORKER_PID" "$BEAT_PID" 2>/dev/null
    wait "$WORKER_PID" "$BEAT_PID" 2>/dev/null
    exit 0
}
trap cleanup INT TERM

echo "🤖 اجرای ربات (Ctrl+C برای توقف کامل همه‌چیز)..."
python3 -m bot.main

# اگه ربات به هر دلیلی متوقف شد، worker و beat هم ببند
cleanup
