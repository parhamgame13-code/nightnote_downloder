#!/bin/bash
# =============================================================
# 🚀 اسکریپت یک‌خطی دیپلوی تعاملی Downloader Bot روی سرور تازه
#
#   ۱) دانلود خودکار پروژه (git clone یا زیپ)
#   ۲) نصب پیش‌نیازهای سیستم (git, python3-venv, ffmpeg, redis, ...)
#   ۳) پرسیدن اطلاعات لازم (BOT_TOKEN، ADMIN_IDS و ...)
#   ۴) تشخیص خودکار IP عمومی و تنظیم لینک مستقیم/دامنه
#   ۵) ساخت کامل .env
#   ۶) اجرای setup_downloader_bot.sh پروژه
#      (کاربر سرویس، Docker + کانتینر Local Bot API، venv،
#       requirements، migration ها و systemd رو اون انجام می‌ده)
#
# استفاده:
#   sudo bash deploy.sh https://github.com/USER/REPO.git
#   sudo bash deploy.sh https://github.com/USER/REPO/archive/refs/heads/main.zip
#
#   # غیرتعاملی (اتوماسیون):
#   REPO_URL="..." BOT_TOKEN="123:ABC" ADMIN_IDS="123,456" NONINTERACTIVE=1 sudo bash deploy.sh
#
# idempotent هست - اجرای چندباره مشکلی نداره.
# =============================================================

set -uo pipefail

REPO_URL="${1:-${REPO_URL:-}}"
BRANCH="${BRANCH:-}"
INSTALL_DIR="${INSTALL_DIR:-/opt/downloader_bot}"
NONINTERACTIVE="${NONINTERACTIVE:-0}"

ok()   { echo -e "✅ $1"; }
warn() { echo -e "⚠️  $1"; }
info() { echo -e "🔹 $1"; }
fail() { echo -e "❌ $1"; }

# سوال تعاملی با پیش‌فرض: ask "سوال" "پیش‌فرض" -> ANSWER
ask() {
    local q="$1" def="${2:-}"
    if [ "$NONINTERACTIVE" = "1" ]; then ANSWER="$def"; return; fi
    if [ -n "$def" ]; then
        read -r -p "$q [$def]: " ANSWER
    else
        read -r -p "$q: " ANSWER
    fi
    ANSWER="${ANSWER:-$def}"
}
# سوال بله/خیر: ask_yn "سوال" 0|1 -> ANSWER
ask_yn() {
    local q="$1" def="${2:-1}"
    if [ "$NONINTERACTIVE" = "1" ]; then ANSWER="$def"; return; fi
    local hint="[Y/n]"; [ "$def" = "0" ] && hint="[y/N]"
    read -r -p "$q $hint: " a
    a="${a:-$([ "$def" = "1" ] && echo y || echo n)}"
    case "$a" in y|Y|yes|Yes) ANSWER=1 ;; *) ANSWER=0 ;; esac
}

echo "=============================================="
echo " دیپلوی خودکار Downloader Bot"
echo "=============================================="

# ---------- 0) فقط روت ----------
if [ "$(id -u)" != "0" ]; then
    fail "این اسکریپت باید با sudo اجرا شه: sudo bash deploy.sh <repo-url>"
    exit 1
fi

# ---------- 1) آدرس مخزن ----------
if [ -z "$REPO_URL" ]; then
    if [ "$NONINTERACTIVE" = "1" ]; then
        fail "آدرس مخزن داده نشد! (REPO_URL=... یا آرگومان اول)"
        exit 1
    fi
    ask "آدرس مخزن (git clone) یا آدرس زیپ"
    REPO_URL="$ANSWER"
fi
info "مخزن: $REPO_URL"
[ -n "$BRANCH" ] && info "شاخه: $BRANCH"

# ---------- 2) نصب پیش‌نیازهای سیستم ----------
SUDO=""
if [ "$(id -u)" != "0" ]; then SUDO="sudo"; fi

APT_PACKAGES=""
command -v git     &>/dev/null || APT_PACKAGES="$APT_PACKAGES git"
command -v python3 &>/dev/null || APT_PACKAGES="$APT_PACKAGES python3 python3-pip"
command -v curl    &>/dev/null || APT_PACKAGES="$APT_PACKAGES curl unzip"
if ! python3 -c "import venv" &>/dev/null 2>&1; then
    APT_PACKAGES="$APT_PACKAGES python3-venv"
fi
command -v ffmpeg  &>/dev/null || APT_PACKAGES="$APT_PACKAGES ffmpeg"
# Redis: صف Celery و کش بهش نیاز دارن (setup_downloader_bot.sh نصبش نمی‌کنه)
command -v redis-server &>/dev/null || APT_PACKAGES="$APT_PACKAGES redis-server"

if [ -n "$APT_PACKAGES" ]; then
    if command -v apt-get &>/dev/null; then
        info "نصب پیش‌نیازهای سیستم:$APT_PACKAGES ..."
        $SUDO apt-get update -y
        $SUDO apt-get install -y $APT_PACKAGES
        ok "پیش‌نیازهای سیستم نصب شدن."
    elif command -v dnf &>/dev/null; then
        info "نصب پیش‌نیازهای سیستم با dnf ..."
        $SUDO dnf install -y git python3 python3-pip curl unzip ffmpeg redis
        ok "پیش‌نیازهای سیستم نصب شدن."
    else
        warn "پکیج‌منیجر پشتیبانی‌شده پیدا نشد - مطمئن شو git/python3-venv/ffmpeg/redis نصبن!"
    fi
else
    ok "پیش‌نیازهای سیستم از قبل نصبن."
fi

# فعال‌سازی Redis
if command -v redis-server &>/dev/null; then
    systemctl enable --now redis-server &>/dev/null || systemctl enable --now redis &>/dev/null || true
    if redis-cli ping &>/dev/null 2>&1; then ok "Redis در حال اجراست."
    else warn "Redis بالا نیامد - دستی چک کن: systemctl status redis-server"; fi
fi

# ---------- 3) دانلود پروژه ----------
cd /opt 2>/dev/null || { $SUDO mkdir -p /opt && cd /opt; }

IS_ZIP=0
case "$REPO_URL" in *.zip) IS_ZIP=1 ;; esac

if [ -d "$INSTALL_DIR/.git" ]; then
    info "پروژه از قبل با گیت هست، در حال آپدیت (git pull)..."
    git -C "$INSTALL_DIR" fetch --all
    if [ -n "$BRANCH" ]; then
        git -C "$INSTALL_DIR" checkout "$BRANCH"
        git -C "$INSTALL_DIR" pull origin "$BRANCH"
    else
        git -C "$INSTALL_DIR" pull
    fi
    ok "کد به‌روز شد."
elif [ -d "$INSTALL_DIR" ] && [ -f "$INSTALL_DIR/bot/main.py" ]; then
    warn "پوشه‌ی $INSTALL_DIR از قبل هست (بدون .git) - دانلود رد شد."
else
    if [ "$IS_ZIP" = "1" ]; then
        info "در حال دانلود و اکسترکت زیپ..."
        $SUDO curl -fL -o /tmp/downloader_bot.zip "$REPO_URL"
        $SUDO rm -rf /tmp/downloader_bot_extract
        $SUDO unzip -q /tmp/downloader_bot.zip -d /tmp/downloader_bot_extract
        INNER=$(find /tmp/downloader_bot_extract -mindepth 1 -maxdepth 1 -type d | head -n 1)
        $SUDO mv "$INNER" "$INSTALL_DIR"
        $SUDO rm -rf /tmp/downloader_bot.zip /tmp/downloader_bot_extract
    else
        info "در حال git clone..."
        if [ -n "$BRANCH" ]; then
            $SUDO git clone --branch "$BRANCH" --single-branch "$REPO_URL" "$INSTALL_DIR"
        else
            $SUDO git clone "$REPO_URL" "$INSTALL_DIR"
        fi
    fi
    ok "پروژه دانلود شد: $INSTALL_DIR"
fi

if [ ! -f "$INSTALL_DIR/bot/main.py" ]; then
    fail "پروژه درست دانلود نشد ($INSTALL_DIR/bot/main.py پیدا نشد)!"
    exit 1
fi

# ---------- 4) تشخیص خودکار IP عمومی ----------
PUBLIC_IP="${PUBLIC_IP:-}"
if [ -z "$PUBLIC_IP" ]; then
    PUBLIC_IP=$(curl -fs -m 8 https://api.ipify.org 2>/dev/null) || \
    PUBLIC_IP=$(curl -fs -m 8 https://ifconfig.me 2>/dev/null) || \
    PUBLIC_IP=$(curl -fs -m 8 https://icanhazip.com 2>/dev/null) || \
    PUBLIC_IP=$(hostname -I 2>/dev/null | awk '{print $1}') || PUBLIC_IP=""
fi
if [ -n "$PUBLIC_IP" ]; then
    ok "IP عمومی سرور تشخیص داده شد: $PUBLIC_IP"
else
    warn "تشخیص IP عمومی ناموفق بود - بعداً دستی واردش کن."
fi

# ---------- 5) سوالات تعاملی ----------
echo ""
echo "=============================================="
echo " سوالات تنظیمات (Enter = پیش‌فرض)"
echo "=============================================="
echo ""

BOT_TOKEN="${BOT_TOKEN:-}"
while [ -z "$BOT_TOKEN" ]; do
    ask "BOT_TOKEN (توکن بات از @BotFather، مثل 123456:ABC-...)"
    BOT_TOKEN="$ANSWER"
    [ -z "$BOT_TOKEN" ] && fail "BOT_TOKEN نمی‌تونه خالی باشه!"
done
if [[ ! "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
    warn "فرمت BOT_TOKEN معمولی نیست - چکش کن که درست کپی شده."
fi

ADMIN_IDS="${ADMIN_IDS:-}"
while [ -z "$ADMIN_IDS" ]; do
    ask "ADMIN_IDS (آی‌دی عددی تلگرامت، از @userinfobot - چندتا با کاما)"
    ADMIN_IDS="$ANSWER"
    [ -z "$ADMIN_IDS" ] && fail "حداقل یک ادمین لازمه!"
done

ask "BOT_USERNAME (یوزرنیم بات بدون @)" "${BOT_USERNAME:-NightDownloaderBot}"
BOT_USERNAME="$ANSWER"
ask "CHANNEL_USERNAME (کانال معرفی‌شده بدون @)" "${CHANNEL_USERNAME:-NightNote_official}"
CHANNEL_USERNAME="$ANSWER"
ask "ALLOWED_GROUP_IDS (گروپ‌های مجاز، با کاما - خالی = همه)" "${ALLOWED_GROUP_IDS:-}"
ALLOWED_GROUP_IDS="$ANSWER"

# دیتابیس
ask_yn "استفاده از PostgreSQL به‌جای SQLite؟ (برای مقیاس واقعی پیشنهاد می‌شه)" 0
if [ "$ANSWER" = "1" ]; then
    if [ "$NONINTERACTIVE" != "1" ] && command -v apt-get &>/dev/null && ! command -v psql &>/dev/null; then
        info "نصب PostgreSQL..."
        $SUDO apt-get install -y postgresql
        systemctl enable --now postgresql
    fi
    ask "DATABASE_URL (اتصال postgres، مثل postgresql+asyncpg://user:pass@localhost/db)"
    DATABASE_URL="$ANSWER"
    warn "حتماً دیتابیس و یوزر رو در postgres بساز."
else
    DATABASE_URL="${DATABASE_URL:-sqlite+aiosqlite:///./bot.db}"
fi
REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"

# لینک مستقیم + دامنه/IP
ask_yn "فعال‌سازی لینک دانلود مستقیم؟ (وب‌سرور اختصاصی + آدرس http://IP:8090)" 1
DIRECT_LINK_ENABLED=$ANSWER
DIRECT_LINK_PUBLIC_URL=""
DIRECT_LINK_PORT=8090
if [ "$DIRECT_LINK_ENABLED" = "1" ]; then
    ask "DIRECT_LINK_PORT (پورت وب‌سرور لینک مستقیم)" "8090"
    DIRECT_LINK_PORT="$ANSWER"
    ask "دامنه‌ی لینک مستقیم (مثل dl.example.ir - خالی = استفاده از IP: ${PUBLIC_IP:-؟})"
    DIRECT_LINK_DOMAIN="$ANSWER"
    if [ -n "$DIRECT_LINK_DOMAIN" ]; then
        DIRECT_LINK_PUBLIC_URL="https://$DIRECT_LINK_DOMAIN"
        warn "پیش‌فرض https ست شد - اگه SSL نداری، توی .env دستی http کن."
    elif [ -n "$PUBLIC_IP" ]; then
        DIRECT_LINK_PUBLIC_URL="http://$PUBLIC_IP:$DIRECT_LINK_PORT"
    fi
fi

# api_id/api_hash تلگرام - جفت عمومی منتشرشده‌ی Telegram Desktop (سورس باز تلگرام)
# همه‌جای اینترنت پخشه و کار می‌کنه؛ شخصی‌سازی لازم نیست.
TELEGRAM_API_ID="${TELEGRAM_API_ID:-17349}"
TELEGRAM_API_HASH="${TELEGRAM_API_HASH:-344583e45741c457fe1862106095a5eb}"

# ---------- 6) ساخت .env ----------
ENV_FILE="$INSTALL_DIR/.env"
info "ساخت/به‌روزرسانی $ENV_FILE ..."
{
    echo "# ساخته‌شده توسط deploy.sh در $(date '+%Y-%m-%d %H:%M:%S')"
    echo ""
    echo "# ---------- تلگرام ----------"
    echo "BOT_TOKEN=$BOT_TOKEN"
    echo "BOT_USERNAME=$BOT_USERNAME"
    echo "CHANNEL_USERNAME=$CHANNEL_USERNAME"
    echo "ADMIN_IDS=$ADMIN_IDS"
    [ -n "$ALLOWED_GROUP_IDS" ] && echo "ALLOWED_GROUP_IDS=$ALLOWED_GROUP_IDS"
    echo ""
    echo "# ---------- صف و دیتابیس ----------"
    echo "REDIS_URL=$REDIS_URL"
    echo "DATABASE_URL=$DATABASE_URL"
    echo ""
    echo "# ---------- Local Bot API Server ----------"
    echo "USE_LOCAL_BOT_API_SERVER=true"
    echo "LOCAL_BOT_API_URL=http://localhost:8081"
    echo "TELEGRAM_API_ID=$TELEGRAM_API_ID"
    echo "TELEGRAM_API_HASH=$TELEGRAM_API_HASH"
    echo ""
    echo "# ---------- لینک دانلود مستقیم ----------"
    echo "DIRECT_LINK_ENABLED=$([ "$DIRECT_LINK_ENABLED" = "1" ] && echo true || echo false)"
    [ -n "$DIRECT_LINK_PUBLIC_URL" ] && echo "DIRECT_LINK_PUBLIC_URL=$DIRECT_LINK_PUBLIC_URL"
    [ "$DIRECT_LINK_ENABLED" = "1" ] && echo "DIRECT_LINK_PORT=$DIRECT_LINK_PORT"
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"
ok ".env ساخته شد."

# ---------- 7) فایروال (اگه ufw فعاله) ----------
if command -v ufw &>/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    info "باز کردن پورت $DIRECT_LINK_PORT در ufw..."
    $SUDO ufw allow "$DIRECT_LINK_PORT/tcp" &>/dev/null
    ok "پورت $DIRECT_LINK_PORT باز شد."
fi

# ---------- 8) اجرای setup اصلی خود پروژه ----------
# (تو deploy/ نگه‌داری می‌شه؛ سازگاری با چیدمان قدیمی ریشه هم حفظ شده)
SETUP_SCRIPT="$INSTALL_DIR/deploy/setup_downloader_bot.sh"
[ -f "$SETUP_SCRIPT" ] || SETUP_SCRIPT="$INSTALL_DIR/setup_downloader_bot.sh"
if [ ! -f "$SETUP_SCRIPT" ]; then
    fail "فایل setup_downloader_bot.sh پیدا نشد (نه تو deploy/ نه تو ریشه)!"
    exit 1
fi
chmod +x "$SETUP_SCRIPT"

# INSTALL_DIR غیرپیش‌فرض → نسخه‌ی اصلاح‌شده‌ی setup
if [ "$INSTALL_DIR" = "/opt/downloader_bot" ]; then
    RUN_SCRIPT="$SETUP_SCRIPT"
else
    RUN_SCRIPT=$(mktemp /tmp/setup_XXXXXX.sh)
    sed "s|INSTALL_DIR=\"/opt/downloader_bot\"|INSTALL_DIR=\"$INSTALL_DIR\"|" \
        "$SETUP_SCRIPT" > "$RUN_SCRIPT"
    chmod +x "$RUN_SCRIPT"
fi

export TG_API_ID="$TELEGRAM_API_ID" TG_API_HASH="$TELEGRAM_API_HASH"
info "اجرای setup_downloader_bot.sh (Docker، کانتینر Local Bot API، venv، requirements، systemd ...)..."
bash "$RUN_SCRIPT"
SETUP_EXIT=$?

[ "$RUN_SCRIPT" != "$SETUP_SCRIPT" ] && rm -f "$RUN_SCRIPT"

if [ $SETUP_EXIT -ne 0 ]; then
    fail "setup با خطا تمام شد (کد $SETUP_EXIT) - لاگ بالا رو چک کن."
    exit $SETUP_EXIT
fi

echo "=============================================="
echo " ✅ دیپلوی کامل شد!"
echo "=============================================="
echo ""
echo "📍 مسیر نصب:      $INSTALL_DIR"
echo "🔒 فایل تنظیمات:  $ENV_FILE"
[ -n "$PUBLIC_IP" ] && echo "🌐 IP عمومی:      $PUBLIC_IP"
[ -n "$DIRECT_LINK_PUBLIC_URL" ] && echo "🔗 لینک مستقیم:   $DIRECT_LINK_PUBLIC_URL"
echo ""
echo "دستورات مفید:"
echo "  sudo systemctl status downloader-bot       # وضعیت سرویس"
echo "  sudo journalctl -u downloader-bot -f       # لاگ زنده"
echo "  sudo systemctl restart downloader-bot      # ری‌استارت بعد از تغییر .env"
echo "  docker logs telegram-bot-api --tail 20     # لاگ Local Bot API"
