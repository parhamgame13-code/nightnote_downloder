#!/bin/bash
# =============================================================
# اسکریپت کامل نصب/چک/تعمیر Downloader Bot روی سرور (جدید یا قدیمی)
# ایمن برای اجرای چندباره‌ست (idempotent) — هر بخش رو فقط اگه لازم بود انجام می‌ده.
# باید با روت اجرا بشه: sudo bash setup_downloader_bot.sh
# =============================================================

set -uo pipefail

PROJECT_DIR="/root/downloader_bot"
VENV_DIR="$PROJECT_DIR/venv"
SERVICE_FILE="$PROJECT_DIR/downloader-bot.service"
SERVICE_TARGET="/etc/systemd/system/downloader-bot.service"

# --- اطلاعات Local Bot API Server (ذخیره‌شده) ---
TG_API_ID="17349"
TG_API_HASH="344583e45741c457fe1862106095a5eb"

ok()   { echo -e "✅ $1"; }
warn() { echo -e "⚠️  $1"; }
info() { echo -e "🔹 $1"; }
fail() { echo -e "❌ $1"; }

echo "=============================================="
echo " شروع بررسی و راه‌اندازی کامل Downloader Bot"
echo "=============================================="

# 1) بررسی وجود پروژه
if [ ! -d "$PROJECT_DIR" ]; then
    fail "پوشه‌ی پروژه پیدا نشد: $PROJECT_DIR — اول زیپ پروژه رو همون‌جا اکسترکت کن."
    exit 1
fi
ok "پوشه‌ی پروژه پیدا شد: $PROJECT_DIR"

# 2) بررسی/نصب Docker
if ! command -v docker &>/dev/null; then
    info "Docker نصب نیست، در حال نصب..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
    ok "Docker نصب و فعال شد."
else
    ok "Docker از قبل نصبه: $(docker --version)"
    systemctl enable --now docker &>/dev/null
fi

# 3) بررسی/ساخت کانتینر Local Bot API Server
mkdir -p /var/lib/telegram-bot-api
if docker ps --format '{{.Names}}' | grep -q '^telegram-bot-api$'; then
    ok "کانتینر telegram-bot-api از قبل در حال اجراست."
elif docker ps -a --format '{{.Names}}' | grep -q '^telegram-bot-api$'; then
    info "کانتینر telegram-bot-api وجود داره ولی خاموشه، در حال استارت..."
    docker start telegram-bot-api &>/dev/null
    sleep 2
    ok "کانتینر telegram-bot-api استارت شد."
else
    info "کانتینر telegram-bot-api وجود نداره، در حال ساخت..."
    docker run -d \
        --name telegram-bot-api \
        --restart always \
        -p 8081:8081 \
        -v /var/lib/telegram-bot-api:/var/lib/telegram-bot-api \
        -e TELEGRAM_API_ID="$TG_API_ID" \
        -e TELEGRAM_API_HASH="$TG_API_HASH" \
        -e TELEGRAM_LOCAL=1 \
        aiogram/telegram-bot-api:latest
    sleep 3
    ok "کانتینر telegram-bot-api ساخته و استارت شد."
fi

sleep 2
if docker ps --format '{{.Names}} {{.Status}}' | grep telegram-bot-api | grep -qi "restarting"; then
    fail "کانتینر telegram-bot-api داره ری‌استارت می‌شه (crash loop). لاگش رو چک کن: docker logs telegram-bot-api --tail 30"
else
    ok "کانتینر telegram-bot-api پایدار و سالمه."
fi

# 4) بررسی/ساخت venv
if [ ! -f "$VENV_DIR/bin/python3" ]; then
    info "venv پیدا نشد، در حال ساخت..."
    python3 -m venv "$VENV_DIR"
    ok "venv ساخته شد."
else
    ok "venv از قبل موجوده."
fi

# 5) نصب کامل requirements
if [ -f "$PROJECT_DIR/requirements.txt" ]; then
    info "در حال نصب/به‌روزرسانی requirements.txt ..."
    "$VENV_DIR/bin/pip" install -q --upgrade pip
    "$VENV_DIR/bin/pip" install -q -r "$PROJECT_DIR/requirements.txt"
    ok "requirements.txt نصب شد."
else
    warn "requirements.txt پیدا نشد، این مرحله رد شد."
fi

# 6) اطمینان از async_timeout (باگ شناخته‌شده‌ی redis<8.0)
if ! "$VENV_DIR/bin/pip" show async_timeout &>/dev/null; then
    info "async_timeout نصب نیست، در حال نصب..."
    "$VENV_DIR/bin/pip" install -q async_timeout
    ok "async_timeout نصب شد."
else
    ok "async_timeout از قبل نصبه."
fi

# 7) اطمینان از وجود اجرایی yt-dlp داخل venv
if [ ! -x "$VENV_DIR/bin/yt-dlp" ]; then
    info "yt-dlp executable پیدا نشد، در حال نصب مجدد..."
    "$VENV_DIR/bin/pip" install -q --force-reinstall yt-dlp
    ok "yt-dlp نصب شد."
else
    ok "yt-dlp executable موجوده: $VENV_DIR/bin/yt-dlp"
fi

# 8) اصلاح فایل سرویس (مسیر پروژه، پایتون venv، PATH)
if [ ! -f "$SERVICE_FILE" ]; then
    fail "فایل $SERVICE_FILE پیدا نشد — این مرحله رد شد، دستی چک کن."
else
    TMP_SERVICE=$(mktemp)
    cp "$SERVICE_FILE" "$TMP_SERVICE"

    # مسیر WorkingDirectory و ExecStart رو با venv و PROJECT_DIR هماهنگ کن
    sed -i \
        -e "s|^WorkingDirectory=.*|WorkingDirectory=$PROJECT_DIR|" \
        -e "s|^ExecStart=.*run\.py|ExecStart=$VENV_DIR/bin/python3 $PROJECT_DIR/run.py|" \
        "$TMP_SERVICE"

    # اضافه کردن PATH که venv/bin اول باشه (برای پیدا شدن yt-dlp توسط subprocessها)
    if ! grep -q "^Environment=PATH=" "$TMP_SERVICE"; then
        sed -i "/^\[Service\]/a Environment=PATH=$VENV_DIR/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" "$TMP_SERVICE"
    fi

    if ! diff -q "$TMP_SERVICE" "$SERVICE_TARGET" &>/dev/null; then
        cp "$TMP_SERVICE" "$SERVICE_FILE"
        cp "$TMP_SERVICE" "$SERVICE_TARGET"
        ok "فایل سرویس اصلاح و کپی شد به $SERVICE_TARGET"
        RELOAD_NEEDED=1
    else
        ok "فایل سرویس از قبل درست بود."
        RELOAD_NEEDED=0
    fi
    rm -f "$TMP_SERVICE"
fi

# 9) enable + (re)start سرویس
systemctl daemon-reload
systemctl enable downloader-bot &>/dev/null

if systemctl is-active --quiet downloader-bot; then
    info "سرویس در حال اجراست، در حال ری‌استارت برای اعمال تغییرات..."
    systemctl restart downloader-bot
else
    info "سرویس خاموش بود، در حال استارت..."
    systemctl start downloader-bot
fi

sleep 6

echo "=============================================="
echo " نتیجه‌ی نهایی"
echo "=============================================="
systemctl status downloader-bot --no-pager -l | head -15

echo ""
echo "----- آخرین خطوط لاگ -----"
if [ -f "$PROJECT_DIR/logs/run_bot.log" ]; then
    tail -n 20 "$PROJECT_DIR/logs/run_bot.log"
fi

if systemctl is-active --quiet downloader-bot; then
    ok "سرویس بات فعال و در حال اجراست."
else
    fail "سرویس بات فعال نیست — لاگ بالا رو چک کن."
fi

echo "=============================================="
echo " تمام شد."
echo "=============================================="
