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

# آیا systemd در دسترسه؟ (روی کانتینرها مثل Deepnote/Docker نیست)
has_systemd() {
    command -v systemctl &>/dev/null && [ -d /run/systemd/system ]
}

# ---------- کمکی‌های .env و پرسش تعاملی (برای وقتی .env ناقصه) ----------
# دیگه وسط کار نمی‌ایستیم که «برو با nano پرش کن» — مقادیر اجباریِ خالی رو
# همین‌جا می‌پرسیم و مستقیم داخل .env می‌نویسیم (بقیه‌ی فایل دست‌نخورده می‌مونه).
ENV_FILE="$INSTALL_DIR/.env"

ask_read() {  # ask_read "prompt" -> ANSWER (از /dev/tty می‌خونه)
    if [ -r /dev/tty ]; then
        read -r -p "$1" ANSWER < /dev/tty
    else
        read -r -p "$1" ANSWER
    fi
}
can_prompt() { [ -r /dev/tty ] || [ -t 0 ]; }

envget() {  # envget KEY [default] — مقدار فعلی از .env (خط فعال یا کامنت‌شده)
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
    has_systemd && systemctl enable --now docker &>/dev/null || true
    ok "Docker نصب و فعال شد."
else
    ok "Docker از قبل نصبه: $(docker --version)"
    has_systemd && systemctl enable --now docker &>/dev/null || true
fi

# اگه dockerd در حال اجرا نیست و systemd هم نداریم، خودمون استارتش می‌کنیم
if ! docker info &>/dev/null; then
    if has_systemd; then
        systemctl start docker &>/dev/null || true
    else
        warn "داِمن Docker در حال اجرا نیست و systemd هم در دسترس نیست."
        warn "اگه تو کانتینر هستی (Docker-in-Docker)، ممکنه Local Bot API قابل اجرا نباشه."
    fi
    sleep 2
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

# 6) .env — اگه نبود از نمونه می‌سازیم؛ مقادیر اجباریِ خالی رو می‌پرسیم و
#    مستقیم داخل فایل می‌نویسیم (نیازی به ویرایش دستی با nano نیست)
if [ ! -f "$ENV_FILE" ]; then
    if [ -f "$INSTALL_DIR/.env.example" ]; then
        cp "$INSTALL_DIR/.env.example" "$ENV_FILE"
        ok "فایل .env از روی .env.example ساخته شد."
    else
        printf '# NightDownloaderBot settings\n' > "$ENV_FILE"
        warn ".env.example پیدا نشد؛ .env خالی ساخته شد."
    fi
fi

if can_prompt; then
    # ----- BOT_TOKEN (اجباری) -----
    CUR_TOKEN=$(envget BOT_TOKEN "")
    case "$CUR_TOKEN" in ""|"123456:ABC-your-token-here") CUR_TOKEN="" ;; esac
    if [ -z "$CUR_TOKEN" ]; then
        while :; do
            ask_read "BOT_TOKEN (توکن بات از @BotFather — مثل 123456:ABC-...): "
            T="${ANSWER:-}"
            if [[ ! "$T" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
                fail "فرمت توکن درست نیست (باید شبیه 123456:ABC-... باشه)."
                continue
            fi
            info "چک کردن توکن با سرور تلگرام ..."
            case "$(check_bot_token "$T")" in
                OK:*)
                    ok "توکن معتبره."
                    break
                    ;;
                INVALID)
                    fail "تلگرام این توکن رو رد کرد — دوباره چکش کن."
                    ;;
                *)
                    warn "اتصال به api.telegram.org برقرار نشد — با همین توکن ادامه می‌دم."
                    break
                    ;;
            esac
        done
        upsert_env BOT_TOKEN "$T"
        ok "BOT_TOKEN داخل .env ست شد."
    else
        ok "BOT_TOKEN از قبل تو .env هست."
    fi

    # ----- ADMIN_IDS (پیشنهادی) -----
    CUR_ADMINS=$(envget ADMIN_IDS "")
    if [ -z "$CUR_ADMINS" ]; then
        ask_read "ADMIN_IDS (آی‌دی عددی تلگرامت از @userinfobot — چندتا با کاما، Enter = رد شو): "
        A="${ANSWER// /}"
        if [ -n "$A" ]; then
            if [[ "$A" =~ ^[0-9]+(,[0-9]+)*$ ]]; then
                upsert_env ADMIN_IDS "$A"
                ok "ADMIN_IDS ست شد: $A"
            else
                warn "فرمت ADMIN_IDS عجیب بود — ست نشد؛ بعداً دستی تو .env پرش کن."
            fi
        else
            warn "ADMIN_IDS خالی موند — دستورهای ادمین تا وقتی تو .env پرش کنی کار نمی‌کنن."
        fi
    else
        ok "ADMIN_IDS از قبل تو .env هست."
    fi
else
    # بدون ترمینال تعاملی (CI/pipe) — فقط چک می‌کنیم و با راهنما می‌شینیم
    NEED=""
    CUR_TOKEN=$(envget BOT_TOKEN "")
    case "$CUR_TOKEN" in ""|"123456:ABC-your-token-here") NEED="BOT_TOKEN" ;; esac
    [ -z "$(envget ADMIN_IDS "")" ] && NEED="${NEED:+$NEED و }ADMIN_IDS"
    if [ -n "$NEED" ]; then
        fail "مقدار $NEED تو .env ست نیست و ترمینال تعاملی هم در دسترس نیست!"
        echo "   یا با env اجرا کن:  BOT_TOKEN=\"123:ABC\" ADMIN_IDS=\"123\" sudo -E bash setup_downloader_bot.sh"
        echo "   یا (پیشنهادی) از deploy.sh استفاده کن که همه‌چیز رو خودش می‌پرسه: deploy/deploy.sh"
        exit 1
    fi
fi

chown "$SERVICE_USER:$SERVICE_USER" "$ENV_FILE"
chmod 600 "$ENV_FILE"
ok "فایل .env آماده و مقادیر اجباری پرن."

# 7) venv (با کاربر سرویس ساخته/استفاده می‌شه تا مالکیت‌ها خراب نشه)
if [ ! -f "$VENV_DIR/bin/python3" ]; then
    info "venv پیدا نشد، در حال ساخت..."
    sudo -u "$SERVICE_USER" -H python3 -m venv "$VENV_DIR"
    ok "venv ساخته شد."
else
    ok "venv از قبل موجوده."
fi

# 8) نصب کامل requirements
if [ -f "$INSTALL_DIR/requirements.txt" ]; then
    info "در حال نصب/به‌روزرسانی requirements.txt ..."
    sudo -u "$SERVICE_USER" -H "$VENV_DIR/bin/pip" install -q --upgrade pip
    sudo -u "$SERVICE_USER" -H "$VENV_DIR/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
    ok "requirements.txt نصب شد."
else
    warn "requirements.txt پیدا نشد، این مرحله رد شد."
fi

# 9) اطمینان از async_timeout (باگ شناخته‌شده‌ی redis<8.0)
if ! sudo -u "$SERVICE_USER" -H "$VENV_DIR/bin/pip" show async_timeout &>/dev/null; then
    info "async_timeout نصب نیست، در حال نصب..."
    sudo -u "$SERVICE_USER" -H "$VENV_DIR/bin/pip" install -q async_timeout
    ok "async_timeout نصب شد."
else
    ok "async_timeout از قبل نصبه."
fi

# 10) اطمینان از وجود اجرایی yt-dlp داخل venv
if [ ! -x "$VENV_DIR/bin/yt-dlp" ]; then
    info "yt-dlp executable پیدا نشد، در حال نصب مجدد..."
    sudo -u "$SERVICE_USER" -H "$VENV_DIR/bin/pip" install -q --force-reinstall yt-dlp
    ok "yt-dlp نصب شد."
else
    ok "yt-dlp executable موجوده: $VENV_DIR/bin/yt-dlp"
fi

# 11) اعمال migration های دیتابیس
info "اعمال migration ها (alembic upgrade head) ..."
if sudo -u "$SERVICE_USER" -H bash -c "cd '$INSTALL_DIR' && '$VENV_DIR/bin/alembic' upgrade head"; then
    ok "دیتابیس به‌روزه."
else
    warn "alembic ناموفق بود - اگه بار اوله و دیتابیس از قبل با create_all ساخته شده، طبیعیه؛ ادامه بده."
fi

# 12) فایل‌های سرویس: مسیرها با INSTALL_DIR هماهنگ و کپی به systemd
# (units تو deploy/systemd/ نگه‌داری می‌شن تا ریشه‌ی پروژه تمیز بمونه)
for UNIT in downloader-bot.service downloader-bot-directlink.service; do
    SRC="$INSTALL_DIR/deploy/systemd/$UNIT"
    # سازگاری با ریپوهای قدیمی که units رو تو ریشه داشتن
    [ -f "$SRC" ] || SRC="$INSTALL_DIR/$UNIT"
    TARGET="/etc/systemd/system/$UNIT"
    if [ ! -f "$SRC" ]; then
        warn "فایل $INSTALL_DIR/deploy/systemd/$UNIT پیدا نشد — این واحد رد شد."
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
if ! has_systemd; then
    warn "systemd در دسترس نیست (کانتینر/Deepnote؟) — از راه‌اندازی سرویس رد شدیم."
    echo ""
    echo "   برای اجرای دستی:"
    echo "   cd $INSTALL_DIR && sudo -u $SERVICE_USER $VENV_DIR/bin/python run.py"
    echo ""
    echo "=============================================="
    echo " نصب کامل شد (بدون سرویس systemd)"
    echo "=============================================="
    echo "📍 مسیر نصب: $INSTALL_DIR"
    echo "🔒 فایل تنظیمات: $INSTALL_DIR/.env"
    exit 0
fi

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
