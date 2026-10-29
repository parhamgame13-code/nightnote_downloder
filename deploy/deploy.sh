#!/bin/bash
# =============================================================
# 🚀 دیپلوی یک‌خطی NightDownloaderBot روی سرور تازه (Ubuntu/Debian)
#
# کارهایی که خودش انجام می‌ده:
#   ۱) نصب پیش‌نیازهای سیستم (git, python3-venv, curl, unzip, ffmpeg, redis)
#   ۲) دانلود پروژه (git clone — اگه از قبل هست git pull / یا زیپ)
#   ۳) پرسیدن تنظیمات لازم و نوشتن مستقیم‌شون داخل .env:
#        - BOT_TOKEN با اعتبارسنجی واقعی از سرور تلگرام (getMe)
#        - ADMIN_IDS و بقیه با پیش‌فرض منطقی
#        - اگه PostgreSQL بخوای، خودش نصب و دیتابیس+کاربر می‌سازه
#      ⚠️ روی اجرای دوباره، تنظیمات قبلیِ .env پاک نمی‌شن — فقط همین
#         کلیدها به‌روز می‌شن (کوکی‌ها و تنظیمات دستی حفظ می‌شن).
#   ۴) نصب کامل: کاربر سرویس + Docker + کانتینر Local Bot API + venv
#      + requirements + migration + systemd + استارت سرویس
#
# استفاده:
#   sudo bash deploy.sh
#   sudo bash deploy.sh https://github.com/USER/REPO.git
#   sudo bash deploy.sh https://github.com/USER/REPO/archive/refs/heads/main.zip
#
#   # غیرتعاملی (اتوماسیون) — حتماً با «sudo -E env» که متغیرها به اسکریپت برسن:
#   sudo -E env REPO_URL="https://github.com/USER/REPO.git" \
#     BOT_TOKEN="123:ABC" ADMIN_IDS="123,456" NONINTERACTIVE=1 bash deploy.sh
#
# idempotent هست؛ اجرای دوباره = آپدیت کد + حفظ تنظیمات .env.
# =============================================================

set -uo pipefail

# اگه کسی با sh اجرا کرد، ببرش تو bash (سینتکس [[ ]] و [[ =~ ]] داریم)
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi

HOME="${HOME:-/root}"

REPO_URL_DEFAULT="https://github.com/parhamgame13-code/nightnote_downloder.git"
REPO_URL="${1:-${REPO_URL:-}}"
BRANCH="${BRANCH:-}"
INSTALL_DIR="${INSTALL_DIR:-/opt/downloader_bot}"
NONINTERACTIVE="${NONINTERACTIVE:-0}"
TG_API_ID="${TG_API_ID:-17349}"
TG_API_HASH="${TG_API_HASH:-344583e45741c457fe1862106095a5eb}"
ENV_FILE="$INSTALL_DIR/.env"

ok()   { echo -e "✅ $1"; }
warn() { echo -e "⚠️  $1"; }
info() { echo -e "🔹 $1"; }
fail() { echo -e "❌ $1"; }

# ---------- پرسش تعاملی ----------
# وقتی با curl | bash اجرا می‌شه، fd 0 خودِ متن اسکریپته؛ از /dev/tty می‌خونیم.
ask_read() {  # ask_read "prompt" -> ANSWER
    if [ -r /dev/tty ]; then
        read -r -p "$1" ANSWER < /dev/tty
    else
        read -r -p "$1" ANSWER
    fi
}
can_prompt() { [ -r /dev/tty ] || [ -t 0 ]; }
has_systemd() { command -v systemctl &>/dev/null && [ -d /run/systemd/system ]; }

ask() {  # ask "سوال" [پیش‌فرض] -> ANSWER
    local q="$1" def="${2:-}"
    if [ "$NONINTERACTIVE" = "1" ]; then ANSWER="$def"; return; fi
    if [ -n "$def" ]; then ask_read "$q [$def]: "; else ask_read "$q: "; fi
    ANSWER="${ANSWER:-$def}"
}
ask_yn() {  # ask_yn "سوال" 0|1 -> ANSWER(0/1)
    local q="$1" def="${2:-1}"
    if [ "$NONINTERACTIVE" = "1" ]; then ANSWER="$def"; return; fi
    local hint="[Y/n]"; [ "$def" = "0" ] && hint="[y/N]"
    ask_read "$q $hint: "
    case "${ANSWER:-}" in n|N|no|No) ANSWER=0 ;; y|Y|yes|Yes) ANSWER=1 ;; *) ANSWER="$def" ;; esac
}

# ---------- .env: خواندن/نوشتن کلید بدون خراب کردن بقیه‌ی فایل ----------
envget() {  # envget KEY [default] — مقدار فعلی از .env (خط فعال یا حتی کامنت‌شده)
    local v=""
    if [ -f "$ENV_FILE" ]; then
        v=$(grep -E "^[[:space:]]*#?[[:space:]]*$1=" "$ENV_FILE" 2>/dev/null | tail -n 1 | cut -d= -f2- | sed -E 's/^[[:space:]]+|[[:space:]]+$//g; s/^"(.*)"$/\1/') || v=""
    fi
    printf '%s' "${v:-${2:-}}"
}
upsert_env() {  # upsert_env KEY VALUE — همه‌ی خطوط این کلید (حتی کامنت‌شده) جایگزین می‌شن
    local key="$1" value="$2"
    if grep -qE "^[[:space:]]*#?[[:space:]]*${key}=" "$ENV_FILE" 2>/dev/null; then
        # آدرسِ تکی (نه بازه‌ای!) — فقط خطوط منطبق با کلید عوض می‌شن
        sed -i -E "/^[[:space:]]*#?[[:space:]]*${key}=/s|.*|${key}=${value}|" "$ENV_FILE"
    else
        printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    fi
}

# ---------- اعتبارسنجی واقعی توکن با getMe ----------
check_bot_token() {  # -> OK:<username> | INVALID | UNREACHABLE
    local resp u
    # بدون -f: پاسخ 401 (توکن غلط) هم باید برسه تا INVALID تشخیص داده شه
    resp=$(curl -s -m 12 "https://api.telegram.org/bot${1}/getMe" 2>/dev/null) || { echo "UNREACHABLE"; return; }
    if printf '%s' "$resp" | grep -q '"ok"[[:space:]]*:[[:space:]]*true'; then
        u=$(printf '%s' "$resp" | grep -oE '"username"[[:space:]]*:[[:space:]]*"[^"]+"' | head -n 1 | sed -E 's/.*:[[:space:]]*"([^"]+)".*/\1/')
        echo "OK:${u:-unknown}"
    else
        echo "INVALID"
    fi
}

# ---------- PostgreSQL: نصب + ساخت خودکار کاربر/دیتابیس ----------
setup_postgres() {
    info "نصب PostgreSQL ..."
    if command -v apt-get &>/dev/null; then
        apt-get install -y postgresql || warn "نصب postgresql ناموفق بود."
    elif command -v dnf &>/dev/null; then
        dnf install -y postgresql-server || warn "نصب postgresql-server ناموفق بود."
        (postgresql-setup --initdb) &>/dev/null || true
    fi
    if has_systemd; then systemctl enable --now postgresql 2>/dev/null || true; fi

    pg_as() {
        if command -v runuser &>/dev/null; then runuser -u postgres -- "$@"
        else sudo -u postgres "$@"; fi
    }
    local i
    for i in $(seq 1 20); do
        pg_as pg_isready -q &>/dev/null && break
        sleep 1
    done
    if ! pg_as pg_isready -q &>/dev/null; then
        warn "PostgreSQL هنوز آماده نیست — ادامه می‌دم؛ اگه alembic خطا داد، setup رو دوباره اجرا کن."
    fi

    local PG_PASS
    PG_PASS=$(tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 24) || true
    if ! pg_as psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='downloader'" 2>/dev/null | grep -q 1; then
        pg_as psql -c "CREATE ROLE downloader LOGIN PASSWORD '$PG_PASS';" || { fail "ساخت کاربر postgres ناموفق بود."; exit 1; }
        ok "کاربر دیتابیس «downloader» ساخته شد."
    else
        pg_as psql -c "ALTER ROLE downloader WITH LOGIN PASSWORD '$PG_PASS';" || true
        ok "کاربر «downloader» از قبل بود — رمز جدیدش ست شد."
    fi
    if ! pg_as psql -tAc "SELECT 1 FROM pg_database WHERE datname='downloader_bot'" 2>/dev/null | grep -q 1; then
        pg_as createdb -O downloader downloader_bot || { fail "ساخت دیتابیس ناموفق بود."; exit 1; }
        ok "دیتابیس «downloader_bot» ساخته شد."
    else
        ok "دیتابیس «downloader_bot» از قبل موجوده."
    fi
    DATABASE_URL="postgresql+asyncpg://downloader:${PG_PASS}@localhost:5432/downloader_bot"
}

echo "=============================================="
echo " دیپلوی خودکار NightDownloaderBot"
echo "=============================================="

# ---------- 0) فقط روت ----------
if [ "$(id -u)" != "0" ]; then
    fail "این اسکریپت باید با sudo اجرا شه: sudo bash deploy.sh"
    exit 1
fi

# ---------- 1) آدرس مخزن ----------
if [ -z "$REPO_URL" ]; then
    if [ "$NONINTERACTIVE" = "1" ]; then
        fail "آدرس مخزن داده نشد! (آرگومان اول یا REPO_URL=...)"
        exit 1
    fi
    ask "آدرس مخزن (git clone) یا آدرس زیپ" "$REPO_URL_DEFAULT"
    REPO_URL="$ANSWER"
fi
info "مخزن: $REPO_URL"
[ -n "$BRANCH" ] && info "شاخه: $BRANCH"

# ---------- 2) پیش‌نیازهای سیستم ----------
APT_PACKAGES=""
command -v git          &>/dev/null || APT_PACKAGES="$APT_PACKAGES git"
command -v curl         &>/dev/null || APT_PACKAGES="$APT_PACKAGES curl unzip"
command -v python3      &>/dev/null || APT_PACKAGES="$APT_PACKAGES python3"
if ! python3 -c "import venv" &>/dev/null 2>&1; then
    APT_PACKAGES="$APT_PACKAGES python3-venv python3-pip"
fi
command -v ffmpeg       &>/dev/null || APT_PACKAGES="$APT_PACKAGES ffmpeg"
command -v redis-server &>/dev/null || APT_PACKAGES="$APT_PACKAGES redis-server"

if [ -n "$APT_PACKAGES" ]; then
    if command -v apt-get &>/dev/null; then
        info "نصب پیش‌نیازهای سیستم:$APT_PACKAGES ..."
        apt-get update -y
        apt-get install -y $APT_PACKAGES || warn "نصب بعضی پکیج‌ها ناموفق بود — ادامه می‌دم."
        ok "پیش‌نیازهای سیستم نصب شدن."
    elif command -v dnf &>/dev/null; then
        info "نصب پیش‌نیازهای سیستم با dnf ..."
        dnf install -y git python3 python3-pip curl unzip ffmpeg redis || warn "نصب بعضی پکیج‌ها ناموفق بود."
        ok "پیش‌نیازهای سیستم نصب شدن (روی dnf، venv داخل پکیج python3 هست)."
    else
        warn "پکیج‌منیجر پشتیبانی‌شده پیدا نشد — مطمئن شو git/python3-venv/ffmpeg/redis نصبن!"
    fi
else
    ok "پیش‌نیازهای سیستم از قبل نصبن."
fi

# فعال‌سازی Redis (روی سیستم‌های بدون systemd با daemonize مستقیم)
if command -v redis-server &>/dev/null; then
    if has_systemd; then
        systemctl enable --now redis-server &>/dev/null || \
        systemctl enable --now redis &>/dev/null || true
    fi
    if ! (redis-cli ping &>/dev/null); then
        info "Redis در حال اجرا نیست، بالا آوردنش با daemonize ..."
        redis-server --daemonize yes --save '' --appendonly no &>/dev/null || true
        sleep 1
    fi
    if (redis-cli ping &>/dev/null); then
        ok "Redis در حال اجراست."
    else
        warn "Redis بالا نیومد — دستی چک کن: redis-server --daemonize yes"
    fi
fi

# ---------- 3) دانلود پروژه ----------
cd /opt 2>/dev/null || { mkdir -p /opt && cd /opt; }

# روی اجرای دوم، مالک پوشه کاربر downloader هست و root بدون این تنظیم
# نمی‌تونه git pull بزنه (خطای dubious ownership)
if command -v git &>/dev/null; then
    git config --global --get-all safe.directory 2>/dev/null | grep -Fqx "$INSTALL_DIR" >/dev/null 2>&1 || \
        git config --global --add safe.directory "$INSTALL_DIR" 2>/dev/null || true
fi

IS_ZIP=0
case "$REPO_URL" in *.zip) IS_ZIP=1 ;; esac

if [ -d "$INSTALL_DIR/.git" ]; then
    info "پروژه از قبل با گیت هست، در حال آپدیت (git pull) ..."
    PULL_OK=1
    git -C "$INSTALL_DIR" fetch --all --quiet || PULL_OK=0
    if [ "$PULL_OK" = "1" ]; then
        if [ -n "$BRANCH" ]; then
            git -C "$INSTALL_DIR" checkout -q "$BRANCH" || PULL_OK=0
            git -C "$INSTALL_DIR" pull -q origin "$BRANCH" || PULL_OK=0
        else
            git -C "$INSTALL_DIR" pull -q || PULL_OK=0
        fi
    fi
    if [ "$PULL_OK" = "1" ]; then
        ok "کد به‌روز شد."
    else
        warn "git pull ناموفق بود (اینترنت/دسترسی؟) — با کدِ موجود ادامه می‌دم."
    fi
elif [ -d "$INSTALL_DIR" ] && [ -f "$INSTALL_DIR/bot/main.py" ]; then
    warn "پوشه‌ی $INSTALL_DIR از قبل هست (بدون .git) — دانلود رد شد."
else
    if [ "$IS_ZIP" = "1" ]; then
        info "در حال دانلود و اکسترکت زیپ ..."
        curl -fL --retry 3 -o /tmp/downloader_bot.zip "$REPO_URL" || { fail "دانلود زیپ ناموفق بود."; exit 1; }
        rm -rf /tmp/downloader_bot_extract && mkdir -p /tmp/downloader_bot_extract
        unzip -q /tmp/downloader_bot.zip -d /tmp/downloader_bot_extract || { fail "اکسترکت زیپ ناموفق بود."; exit 1; }
        rm -rf "$INSTALL_DIR"
        # بعضی زیپ‌ها فایل‌ها رو مستقیم تو ریشه دارن نه تو یه پوشه‌ی داخلی
        INNER=$(find /tmp/downloader_bot_extract -mindepth 1 -maxdepth 1 -type d 2>/dev/null | head -n 1)
        ENTRY_COUNT=$(find /tmp/downloader_bot_extract -mindepth 1 -maxdepth 1 2>/dev/null | wc -l)
        if [ -n "$INNER" ] && [ "$ENTRY_COUNT" = "1" ]; then
            mv "$INNER" "$INSTALL_DIR"
        else
            mkdir -p "$INSTALL_DIR"
            cp -a /tmp/downloader_bot_extract/. "$INSTALL_DIR/"
        fi
        rm -rf /tmp/downloader_bot.zip /tmp/downloader_bot_extract
    else
        info "در حال git clone ..."
        rm -rf "$INSTALL_DIR"
        if [ -n "$BRANCH" ]; then
            git clone --branch "$BRANCH" --single-branch "$REPO_URL" "$INSTALL_DIR" || { fail "clone ناموفق بود — آدرس/دسترسی مخزن رو چک کن."; exit 1; }
        else
            git clone "$REPO_URL" "$INSTALL_DIR" || { fail "clone ناموفق بود — آدرس/دسترسی مخزن رو چک کن."; exit 1; }
        fi
    fi
    ok "پروژه آماده‌ست: $INSTALL_DIR"
fi

[ -f "$INSTALL_DIR/bot/main.py" ] || { fail "پروژه درست دانلود نشد ($INSTALL_DIR/bot/main.py پیدا نشد)！"; exit 1; }

# ---------- 4) تشخیص خودکار IP عمومی ----------
PUBLIC_IP="${PUBLIC_IP:-}"
if [ -z "$PUBLIC_IP" ]; then
    PUBLIC_IP=$(curl -fs -m 8 https://api.ipify.org 2>/dev/null) || \
    PUBLIC_IP=$(curl -fs -m 8 https://ifconfig.me 2>/dev/null) || \
    PUBLIC_IP=$(curl -fs -m 8 https://icanhazip.com 2>/dev/null) || \
    PUBLIC_IP=$(hostname -I 2>/dev/null | awk '{print $1}') || PUBLIC_IP=""
fi
if [ -n "$PUBLIC_IP" ]; then
    ok "IP عمومی سرور: $PUBLIC_IP"
else
    warn "تشخیص IP عمومی ناموفق بود."
fi

# ---------- 5) سوالات ----------
if ! can_prompt && [ "$NONINTERACTIVE" != "1" ]; then
    warn "ترمینال تعاملی در دسترس نیست (اجرا با pipe/CI)."
    echo "   مقادیر رو با env بده و با «sudo -E env» اجرا کن، مثلاً:"
    echo "   sudo -E env REPO_URL=\"$REPO_URL\" BOT_TOKEN=\"123:ABC\" ADMIN_IDS=\"123456789\" NONINTERACTIVE=1 bash deploy.sh"
    echo "   یا اسکریپت رو دانلود کن و مستقیم (نه با pipe) اجرا کن."
    if [ -z "${BOT_TOKEN:-}" ] || [ -z "${ADMIN_IDS:-}" ]; then
        NONINTERACTIVE=1
    fi
fi

echo ""
echo "=============================================="
echo " سوالات تنظیمات (Enter = پیش‌فرض / مقدار قبلی)"
echo "=============================================="
echo ""

# ----- BOT_TOKEN (اجباری، با چک آنلاین) -----
BOT_TOKEN="${BOT_TOKEN:-$(envget BOT_TOKEN "")}"
BOT_TG_USERNAME=""
if [ -n "$BOT_TOKEN" ]; then
    echo "توکن فعلی .env: ${BOT_TOKEN:0:12}... (Enter = همین بمونه)"
fi
if [ "$NONINTERACTIVE" = "1" ] && [ -z "$BOT_TOKEN" ]; then
    fail "BOT_TOKEN خالیه — با env بده: BOT_TOKEN=\"123:ABC\""
    exit 1
fi
while :; do
    if [ -z "$BOT_TOKEN" ]; then
        ask "BOT_TOKEN (توکن بات از @BotFather — مثل 123456:ABC-DEF...)"
        BOT_TOKEN="$ANSWER"
        if [ -z "$BOT_TOKEN" ]; then
            fail "BOT_TOKEN نمی‌تونه خالی باشه!"
            [ "$NONINTERACTIVE" = "1" ] && exit 1
            continue
        fi
    fi
    if [[ ! "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
        fail "فرمت توکن درست نیست (باید شبیه 123456:ABC-... باشه)."
        if [ "$NONINTERACTIVE" = "1" ]; then exit 1; fi
        BOT_TOKEN=""
        continue
    fi
    info "چک کردن توکن با سرور تلگرام ..."
    RESULT=$(check_bot_token "$BOT_TOKEN")
    case "$RESULT" in
        OK:*)
            BOT_TG_USERNAME="${RESULT#OK:}"
            ok "توکن معتبره — بات: @${BOT_TG_USERNAME}"
            break
            ;;
        INVALID)
            fail "تلگرام این توکن رو رد کرد (اشتباهه یا ریووک شده)."
            if [ "$NONINTERACTIVE" = "1" ]; then exit 1; fi
            BOT_TOKEN=""
            ;;
        *)
            warn "اتصال به api.telegram.org برقرار نشد — اعتبارسنجی آنلاین رد شد، با همین توکن ادامه می‌دم."
            break
            ;;
    esac
done

# ----- ADMIN_IDS (اجباری) -----
ADMIN_IDS="${ADMIN_IDS:-$(envget ADMIN_IDS "")}"
if [ "$NONINTERACTIVE" = "1" ] && [ -z "$ADMIN_IDS" ]; then
    fail "ADMIN_IDS خالیه — با env بده: ADMIN_IDS=\"123,456\""
    exit 1
fi
while :; do
    if [ -z "$ADMIN_IDS" ]; then
        ask "ADMIN_IDS (آی‌دی عددی تلگرامت از @userinfobot — چندتا با کاما)"
        ADMIN_IDS="$ANSWER"
        if [ -z "$ADMIN_IDS" ]; then
            fail "حداقل یک ادمین لازمه (دستورهای ادمین بدون این کار نمی‌کنه)!"
            [ "$NONINTERACTIVE" = "1" ] && exit 1
            continue
        fi
    fi
    if [[ ! "$ADMIN_IDS" =~ ^[0-9]+(,[0-9]+)*$ ]]; then
        fail "فقط آی‌دی عددی با کاما (مثلاً 123456789 یا 111,222)."
        if [ "$NONINTERACTIVE" = "1" ]; then exit 1; fi
        ADMIN_IDS=""
        continue
    fi
    break
done

# ----- بقیه‌ی تنظیمات تلگرام -----
ask "BOT_USERNAME (یوزرنیم بات بدون @)" "$(envget BOT_USERNAME NightDownloaderBot)"
BOT_USERNAME="$ANSWER"
ask "CHANNEL_USERNAME (کانال معرفی‌شده بدون @)" "$(envget CHANNEL_USERNAME NightNote_official)"
CHANNEL_USERNAME="$ANSWER"
ask "ALLOWED_GROUP_IDS (گروپ‌های مجاز با کاما — خالی = همه)" "$(envget ALLOWED_GROUP_IDS "")"
ALLOWED_GROUP_IDS="$ANSWER"

# ----- دیتابیس -----
CUR_DB_URL="$(envget DATABASE_URL "")"
DB_DEF=0
case "$CUR_DB_URL" in postgresql*) DB_DEF=1 ;; esac
if [ -n "$CUR_DB_URL" ]; then
    case "$CUR_DB_URL" in
        postgresql*) echo "دیتابیس فعلی: PostgreSQL" ;;
        sqlite*)     echo "دیتابیس فعلی: SQLite" ;;
        *)           echo "دیتابیس فعلی: سفارشی" ;;
    esac
fi
ask_yn "PostgreSQL به‌جای SQLite؟ (برای استفاده‌ی واقعی پیشنهاد می‌شه — نصب و ساخت دیتابیس خودکارم)" "$DB_DEF"
if [ "$ANSWER" = "1" ]; then
    setup_postgres
else
    DATABASE_URL="${DATABASE_URL:-sqlite+aiosqlite:///./bot.db}"
fi
REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"

# ----- لینک دانلود مستقیم -----
DL_CUR="$(envget DIRECT_LINK_ENABLED true)"
DL_DEF_NUM=0
[ "$DL_CUR" = "true" ] && DL_DEF_NUM=1
ask_yn "لینک دانلود مستقیم فعال باشه؟ (وب‌سرور اختصاصی + QR کد)" "$DL_DEF_NUM"
DIRECT_LINK_ENABLED=$ANSWER
DIRECT_LINK_PORT="$(envget DIRECT_LINK_PORT 8090)"
DIRECT_LINK_PUBLIC_URL=""
if [ "$DIRECT_LINK_ENABLED" = "1" ]; then
    ask "DIRECT_LINK_PORT (پورت وب‌سرور لینک مستقیم)" "$DIRECT_LINK_PORT"
    DIRECT_LINK_PORT="$ANSWER"
    ask "دامنه‌ی لینک مستقیم (مثل dl.example.ir — خالی = http://${PUBLIC_IP:-IP}:${DIRECT_LINK_PORT})"
    DL_DOMAIN="$ANSWER"
    if [ -n "$DL_DOMAIN" ]; then
        ask_yn "دامنه SSL/https داره؟" 0
        if [ "$ANSWER" = "1" ]; then DIRECT_LINK_PUBLIC_URL="https://$DL_DOMAIN"
        else DIRECT_LINK_PUBLIC_URL="http://$DL_DOMAIN"; fi
        warn "برای https پشت nginx/caddy با گواهی SSL بذارش."
    elif [ -n "$PUBLIC_IP" ]; then
        DIRECT_LINK_PUBLIC_URL="http://$PUBLIC_IP:$DIRECT_LINK_PORT"
        warn "بدون دامنه، لینک‌ها http://IP:PORT می‌شن."
    else
        warn "IP عمومی تشخیص داده نشد — DIRECT_LINK_PUBLIC_URL رو بعداً دستی تو .env ست کن."
    fi
fi

# ---------- 6) خلاصه + تأیید ----------
DB_LABEL="SQLite"
case "$DATABASE_URL" in postgresql*) DB_LABEL="PostgreSQL" ;; esac
echo ""
echo "=============================================="
echo " خلاصه‌ی تنظیمات"
echo "=============================================="
echo "  مخزن:              $REPO_URL"
echo "  مسیر نصب:          $INSTALL_DIR"
echo "  BOT_TOKEN:         ${BOT_TOKEN:0:12}... ${BOT_TG_USERNAME:+(تأییدشده: @${BOT_TG_USERNAME})}"
echo "  ADMIN_IDS:         $ADMIN_IDS"
echo "  BOT_USERNAME:      $BOT_USERNAME"
echo "  CHANNEL_USERNAME:  $CHANNEL_USERNAME"
echo "  ALLOWED_GROUP_IDS: ${ALLOWED_GROUP_IDS:-(همه‌ی گروپ‌ها)}"
echo "  دیتابیس:           $DB_LABEL"
echo "  Redis:             $REDIS_URL"
echo "  Local Bot API:     روشن (فایل تا ۲ گیگ)"
echo "  لینک مستقیم:       $([ "$DIRECT_LINK_ENABLED" = "1" ] && echo "روشن — ${DIRECT_LINK_PUBLIC_URL:-(بدون URL عمومی)}" || echo خاموش)"
echo "=============================================="
if [ "$NONINTERACTIVE" != "1" ]; then
    ask_yn "همه‌چیز درسته؟ شروع نصب؟" 1
    if [ "$ANSWER" != "1" ]; then
        warn "لغو شد — هیچ تغییری اعمال نشد."
        exit 0
    fi
fi

# ---------- 7) نوشتن .env (بدون خراب کردن بقیه‌ی تنظیمات) ----------
info "نوشتن تنظیمات در $ENV_FILE ..."
if [ ! -f "$ENV_FILE" ]; then
    if [ -f "$INSTALL_DIR/.env.example" ]; then
        cp "$INSTALL_DIR/.env.example" "$ENV_FILE"
    else
        printf '# NightDownloaderBot settings — ساخته‌شده توسط deploy.sh\n' > "$ENV_FILE"
    fi
fi
upsert_env BOT_TOKEN "$BOT_TOKEN"
upsert_env BOT_USERNAME "$BOT_USERNAME"
upsert_env CHANNEL_USERNAME "$CHANNEL_USERNAME"
upsert_env ADMIN_IDS "$ADMIN_IDS"
[ -n "$ALLOWED_GROUP_IDS" ] && upsert_env ALLOWED_GROUP_IDS "$ALLOWED_GROUP_IDS"
upsert_env DATABASE_URL "$DATABASE_URL"
upsert_env REDIS_URL "$REDIS_URL"
upsert_env USE_LOCAL_BOT_API_SERVER true
upsert_env LOCAL_BOT_API_URL http://localhost:8081
upsert_env TELEGRAM_API_ID "$TG_API_ID"
upsert_env TELEGRAM_API_HASH "$TG_API_HASH"
upsert_env DIRECT_LINK_ENABLED "$([ "$DIRECT_LINK_ENABLED" = "1" ] && echo true || echo false)"
if [ "$DIRECT_LINK_ENABLED" = "1" ]; then
    [ -n "$DIRECT_LINK_PUBLIC_URL" ] && upsert_env DIRECT_LINK_PUBLIC_URL "$DIRECT_LINK_PUBLIC_URL"
    upsert_env DIRECT_LINK_PORT "$DIRECT_LINK_PORT"
fi
chmod 600 "$ENV_FILE"
ok ".env آماده شد (فقط کلیدهای بالا به‌روز شدن — بقیه‌ی تنظیمات دست‌نخورده)."

# ---------- 8) فایروال ----------
if [ "$DIRECT_LINK_ENABLED" = "1" ] && command -v ufw &>/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    info "باز کردن پورت $DIRECT_LINK_PORT در ufw ..."
    ufw allow "$DIRECT_LINK_PORT/tcp" &>/dev/null && ok "پورت $DIRECT_LINK_PORT باز شد."
fi

# ---------- 9) اجرای نصب نهایی (setup خود پروژه) ----------
SETUP_SCRIPT="$INSTALL_DIR/deploy/setup_downloader_bot.sh"
[ -f "$SETUP_SCRIPT" ] || SETUP_SCRIPT="$INSTALL_DIR/setup_downloader_bot.sh"
if [ ! -f "$SETUP_SCRIPT" ]; then
    fail "فایل setup_downloader_bot.sh پیدا نشد (نه تو deploy/ نه تو ریشه)!"
    exit 1
fi
chmod +x "$SETUP_SCRIPT"

RUN_SCRIPT="$SETUP_SCRIPT"
if [ "$INSTALL_DIR" != "/opt/downloader_bot" ]; then
    # مسیر نصب غیرپیش‌فرض → نسخه‌ی اصلاح‌شده‌ی setup
    RUN_SCRIPT=$(mktemp /tmp/setup_XXXXXX.sh)
    sed "s|INSTALL_DIR=\"/opt/downloader_bot\"|INSTALL_DIR=\"$INSTALL_DIR\"|" "$SETUP_SCRIPT" > "$RUN_SCRIPT"
    chmod +x "$RUN_SCRIPT"
fi

export TG_API_ID TG_API_HASH
info "اجرای نصب نهایی (Docker، کانتینر Local Bot API، venv، requirements، migration، systemd) ..."
bash "$RUN_SCRIPT"
SETUP_EXIT=$?

[ "$RUN_SCRIPT" != "$SETUP_SCRIPT" ] && rm -f "$RUN_SCRIPT"

if [ "$SETUP_EXIT" -ne 0 ]; then
    fail "نصب با خطا تمام شد (کد $SETUP_EXIT) — لاگ بالا رو چک کن."
    exit "$SETUP_EXIT"
fi

# ---------- 10) پایان ----------
echo ""
echo "=============================================="
echo " ✅ دیپلوی کامل شد!"
echo "=============================================="
[ -n "$BOT_TG_USERNAME" ] && echo "🤖 بات @${BOT_TG_USERNAME} آماده‌ست — تو تلگرام بهش /start بده!"
echo "📍 مسیر نصب:    $INSTALL_DIR"
echo "🔒 تنظیمات:     $ENV_FILE"
[ -n "$PUBLIC_IP" ] && echo "🌐 IP عمومی:    $PUBLIC_IP"
if [ "$DIRECT_LINK_ENABLED" = "1" ] && [ -n "$DIRECT_LINK_PUBLIC_URL" ]; then
    echo "🔗 لینک مستقیم: $DIRECT_LINK_PUBLIC_URL"
fi
echo ""
echo "دستورات مفید:"
if has_systemd; then
    echo "  sudo systemctl status downloader-bot      # وضعیت سرویس"
    echo "  sudo journalctl -u downloader-bot -f      # لاگ زنده"
    echo "  sudo systemctl restart downloader-bot     # بعد از تغییر .env"
else
    echo "  (systemd در دسترس نیست — اجرای دستی:)"
    echo "  cd $INSTALL_DIR && sudo -u downloader $INSTALL_DIR/venv/bin/python run.py"
fi
echo "  docker logs telegram-bot-api --tail 20    # لاگ Local Bot API"
