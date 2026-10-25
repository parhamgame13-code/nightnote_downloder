#!/bin/bash
# =============================================================
# اسکریپت کامل نصب/چک/تعمیر Downloader Bot روی سرور (جدید یا قدیمی)
# ایمن برای اجرای چندباره‌ست (idempotent) — هر بخش رو فقط اگه لازم بود انجام می‌ده.
# باید با روت اجرا بشه: sudo bash setup_downloader_bot.sh
#
# مدل استقرار (هماهنگ با downloader-bot.service):
#   - کد تو INSTALL_DIR (پیش‌فرض /opt/downloader_bot)
#   - اجرا با کاربر سیستمیِ بدون لاگینِ «downloader» (نه root!)
#   - دیتای پایدار تو /var/lib/downloader_bot و /var/lib/telegram-bot-api
#   - .env از .env.example ساخته می‌شه (فقط BOT_TOKEN/ADMIN_IDS رو پر کن)
#   -migration ها با alembic اعمال می‌شن
# =============================================================

set -uo pipefail

INSTALL_DIR="/opt/downloader_bot"
SERVICE_USER="downloader"
VENV_DIR="$INSTALL_DIR/venv"

# --- اطلاعات Local Bot API Server (جفت عمومی Telegram Desktop؛ قابل بازنویسی) ---
TG_API_ID="${TG_API_ID:-17349}"
TG_API_HASH="${TG_API_HASH:-344583e45741c457fe1862106095a5eb}"

ok()   { echo -e "✅ $1"; }
warn() { echo -e "⚠️  $1"; }
info() { echo -e "🔹 $1"; }
fail() { echo -e "❌ $1"; }

echo "=============================================="
echo " شروع بررسی و راه‌اندازی کامل Downloader Bot"
echo "=============================================="

# 0) فقط روت
if [ "$(id -u)" != "0" ]; then
    fail "این اسکریپت باید با sudo اجرا شه: sudo bash setup_downloader_bot.sh"
    exit 1
fi

# 1) بررسی وجود پروژه
if [ ! -d "$INSTALL_DIR" ]; then
    fail "پوشه‌ی پروژه پیدا نشد: $INSTALL_DIR"
    echo "   پروژه رو همون‌جا بذار، یا اول این متغیر رو بالای اسکریپت عوض کن:"
    echo "   INSTALL_DIR=\"/مسیر/خودت\""
    exit 1
fi
ok "پوشه‌ی پروژه پیدا شد: $INSTALL_DIR"

# 2) کاربر سیستمیِ بدون لاگین (اجرای سرویس با root یعنی هر باگ = دسترسی کامل سرور)
if id "$SERVICE_USER" &>/dev/null; then
    ok "کاربر $SERVICE_USER از قبل وجود داره."
else
    info "ساخت کاربر سیستمی $SERVICE_USER ..."
    useradd -r -s /usr/sbin/nologin "$SERVICE_USER"
    ok "کاربر $SERVICE_USER ساخته شد."
fi

# 3) پوشه‌های داده + مالکیت‌ها
mkdir -p /var/lib/downloader_bot /var/lib/telegram-bot-api "$INSTALL_DIR/logs"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR" /var/lib/downloader_bot
chown "$SERVICE_USER:$SERVICE_USER" /var/lib/telegram-bot-api
ok "پوشه‌های داده و مالکیت‌ها درست شد."

# 4) بررسی/نصب Docker
if ! command -v docker &>/dev/null; then
    info "Docker نصب نیست، در حال نصب..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
    ok "Docker نصب و فعال شد."
else
    ok "Docker از قبل نصبه: $(docker --version)"
    systemctl enable --now docker &>/dev/null
fi

# 5) بررسی/ساخت کانتینر Local Bot API Server (برای فایل‌های تا ۲ گیگ)
if docker ps --format '{{.Names}}' | grep -q '^telegram-bot-api$'; then
    ok "کانتینر telegram-bot-api از قبل در حال اجراست."
elif docker ps -a --format '{{.Names}}' | grep -q '^telegram-bot-api$'; then
    info "کانتینر telegram-bot-api وجود داره ولی خاموشه، در حال استارت..."
    docker start telegram-bot-api &>/dev/null
    sleep 2
    ok "کانتینر telegram-bot-api استارت شد."
else
    info "کانتینر telegram-bot-api وجود نداره، در حال ساخت..."
    # پورت فقط روی localhost هاست - از اینترنت بیرون بسته‌ست
    docker run -d \
        --name telegram-bot-api \
        --restart always \
        -p 127.0.0.1:8081:8081 \
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

# 6) .env (اگه نبود از نمونه می‌سازیم و می‌ایستیم تا پرش کنی)
if [ ! -f "$INSTALL_DIR/.env" ]; then
    if [ -f "$INSTALL_DIR/.env.example" ]; then
        cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
        chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/.env"
        chmod 600 "$INSTALL_DIR/.env"
        fail "فایل .env از روی .env.example ساخته شد ولی خالیه!"
        echo "   الان این فایل رو باز کن و حداقل BOT_TOKEN و ADMIN_IDS رو پر کن:"
        echo "   nano $INSTALL_DIR/.env"
        echo "   بعد دوباره این اسکریپت رو اجرا کن."
        exit 1
    else
        fail "نه .env هست نه .env.example — دستی درستش کن."
        exit 1
    fi
else
    ok "فایل .env موجوده."
fi

# 7) venv (با کاربر سرویس ساخته/استفاده می‌شه تا مالکیت‌ها خراب نشه)
if [ ! -f "$VENV_DIR/bin/python3" ]; then
    info "venv پیدا نشد، در حال ساخت..."
    sudo -u "$SERVICE_USER" python3 -m venv "$VENV_DIR"
    ok "venv ساخته شد."
else
    ok "venv از قبل موجوده."
fi

# 8) نصب کامل requirements
if [ -f "$INSTALL_DIR/requirements.txt" ]; then
    info "در حال نصب/به‌روزرسانی requirements.txt ..."
    sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install -q --upgrade pip
    sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
    ok "requirements.txt نصب شد."
else
    warn "requirements.txt پیدا نشد، این مرحله رد شد."
fi

# 9) اطمینان از async_timeout (باگ شناخته‌شده‌ی redis<8.0)
if ! sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" show async_timeout &>/dev/null; then
    info "async_timeout نصب نیست، در حال نصب..."
    sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install -q async_timeout
    ok "async_timeout نصب شد."
else
    ok "async_timeout از قبل نصبه."
fi

# 10) اطمینان از وجود اجرایی yt-dlp داخل venv
if [ ! -x "$VENV_DIR/bin/yt-dlp" ]; then
    info "yt-dlp executable پیدا نشد، در حال نصب مجدد..."
    sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install -q --force-reinstall yt-dlp
    ok "yt-dlp نصب شد."
else
    ok "yt-dlp executable موجوده: $VENV_DIR/bin/yt-dlp"
fi

# 11) اعمال migration های دیتابیس
info "اعمال migration ها (alembic upgrade head) ..."
if sudo -u "$SERVICE_USER" bash -c "cd '$INSTALL_DIR' && '$VENV_DIR/bin/alembic' upgrade head"; then
    ok "دیتابیس به‌روزه."
else
    warn "alembic ناموفق بود - اگه بار اوله و دیتابیس از قبل با create_all ساخته شده، طبیعیه؛ ادامه بده."
fi

# 12) فایل‌های سرویس: مسیرها با INSTALL_DIR هماهنگ و کپی به systemd
for UNIT in downloader-bot.service downloader-bot-directlink.service; do
    SRC="$INSTALL_DIR/$UNIT"
    TARGET="/etc/systemd/system/$UNIT"
    if [ ! -f "$SRC" ]; then
        warn "فایل $SRC پیدا نشد — این واحد رد شد."
        continue
    fi
    TMP_UNIT=$(mktemp)
    # مسیر پیش‌فرض units (/opt/downloader_bot) با INSTALL_DIR این سرور جایگزین می‌شه
    sed "s|/opt/downloader_bot|$INSTALL_DIR|g" "$SRC" > "$TMP_UNIT"
    # اجرا با pythonِ venv (نه system python)
    sed -i -e "s|ExecStart=/usr/bin/python3|ExecStart=$VENV_DIR/bin/python3|" "$TMP_UNIT"
    cp "$TMP_UNIT" "$TARGET"
    rm -f "$TMP_UNIT"
    ok "$UNIT آماده شد → $TARGET"
done

# 13) enable + (re)start سرویس اصلی
# (وب‌سرور لینک مستقیم وقتی DIRECT_LINK_ENABLED فعاله توسط run.py خودش بالا
#  میاد؛ واحد downloader-bot-directlink فقط برای اجرای جداگانه‌ست - دستی enableش کن)
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
if [ -f "$INSTALL_DIR/logs/run_bot.log" ]; then
    tail -n 20 "$INSTALL_DIR/logs/run_bot.log"
fi

if systemctl is-active --quiet downloader-bot; then
    ok "سرویس بات فعال و در حال اجراست."
else
    fail "سرویس بات فعال نیست — لاگ بالا رو چک کن."
fi

echo "=============================================="
echo " تمام شد."
echo "=============================================="
