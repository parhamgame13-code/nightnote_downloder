# 🚀 اسکریپت‌های استقرار (Deployment)

این پوشه همه‌ی چیزهای مربوط به نصب و اجرای پروژه روی سرور رو نگه می‌داره،
تا ریشه‌ی ریپو فقط شامل کد اصلی و فایل‌های پیکربندی ابزارها باشه.

```
deploy/
├── deploy.sh                    # دیپلوی کامل تعاملی (دانلود + نصب + .env)
├── setup_downloader_bot.sh      # نصب/تعمیر idempotent (بدون دانلود)
└── systemd/
    ├── downloader-bot.service            # بات + worker + beat (run.py)
    └── downloader-bot-directlink.service # وب‌سرور لینک مستقیم (اختیاری)
```

---

## ۱️⃣ `deploy.sh` — دیپلوی کامل با یک دستور (پیشنهادی)

برای سرور **تازه** که هنوز هیچی روش نیست. خودش:

1. پروژه رو `git clone` می‌کنه (یا زیپ دانلود می‌کنه) — اگه از قبل باشه `git pull`
2. پیش‌نیازهای سیستم رو نصب می‌کنه: `git`, `python3-venv`, `curl`, `unzip`,
   `ffmpeg`, `redis-server` (+ فعال‌سازی Redis)
3. **تعاملی** اطلاعات لازم رو می‌پرسه (Enter = پیش‌فرض):
   - `BOT_TOKEN` (اجباری، از @BotFather)
   - `ADMIN_IDS` (اجباری، از @userinfobot)
   - `BOT_USERNAME` / `CHANNEL_USERNAME` / `ALLOWED_GROUP_IDS`
   - دیتابیس: SQLite (پیش‌فرض) یا PostgreSQL (نصبش هم خودکار)
   - لینک مستقیم: روشن/خاموش + پورت + دامنه
4. **IP عمومی سرور رو خودکار تشخیص می‌ده** (api.ipify.org → ifconfig.me →
   icanhazip.com → `hostname -I`) و اگه دامنه ندی،
   `DIRECT_LINK_PUBLIC_URL` رو `http://<IP>:8090` می‌کنه
5. `.env` کامل می‌سازه (با `chmod 600`)
6. اگه `ufw` فعال باشه پورت لینک مستقیم رو باز می‌کنه
7. `setup_downloader_bot.sh` رو صدا می‌زنه که بقیه‌ی کار رو تموم کنه

### اجرا

```bash
# از داخل ریپوی کلون‌شده:
sudo bash deploy/deploy.sh https://github.com/USER/REPO.git

# یا بدون کلون قبلی (مستقیم از گیت‌هاب):
curl -fsSL https://raw.githubusercontent.com/USER/REPO/main/deploy/deploy.sh \
  | sudo bash -s -- https://github.com/USER/REPO.git

# با زیپ:
sudo bash deploy/deploy.sh https://github.com/USER/REPO/archive/refs/heads/main.zip

# شاخه یا مسیر نصب دلخواه:
BRANCH=dev INSTALL_DIR=/opt/downloader_bot sudo bash deploy/deploy.sh <repo-url>

# غیرتعاملی (اتوماسیون / CI):
REPO_URL="..." BOT_TOKEN="123:ABC" ADMIN_IDS="123,456" \
  DIRECT_LINK_DOMAIN="dl.example.ir" NONINTERACTIVE=1 \
  sudo bash deploy/deploy.sh
```

> **idempotent** هست — اجرای چندباره مشکلی نداره و برای آپدیت هم می‌شه
> دوباره اجراش کرد.

---

## ۲️⃣ `setup_downloader_bot.sh` — فقط نصب (پروژه از قبل هست)

وقتی کد رو خودت گذاشتی سر جاش (git clone / scp / zip) و فقط می‌خوای
نصب/تعمیر بشه. کارهایی که می‌کنه:

- کاربر سیستمیِ بدون لاگین `downloader` (نه root!)
- پوشه‌های داده: `/var/lib/downloader_bot`, `/var/lib/telegram-bot-api`
- Docker + کانتینر Local Bot API Server (پورت 8081، فقط localhost)
- venv + نصب `requirements.txt`
- ساخت `.env` از `.env.example` (بار اول می‌ایسته تا پرش کنی)
- `alembic upgrade head` (migration ها)
- کپی و تنظیم unit های systemd از `deploy/systemd/`
- `systemctl enable` + `restart`

```bash
sudo bash deploy/setup_downloader_bot.sh

# مسیر نصب دلخواه (پیش‌فرض /opt/downloader_bot):
sudo INSTALL_DIR=/srv/downloader_bot bash deploy/setup_downloader_bot.sh
```

---

## ۳️⃣ systemd units (`deploy/systemd/`)

| فایل | کاربرد |
|------|--------|
| `downloader-bot.service` | سرویس اصلی — بات + Celery worker + beat (همه با `run.py`) |
| `downloader-bot-directlink.service` | وب‌سرور لینک مستقیم (فقط اگه `DIRECT_LINK_ENABLED=true` و نمی‌خوای `run.py` بالا بیارش) |

هر دو با کاربر غیر-root `downloader` و hardening (`ProtectSystem=strict`,
`NoNewPrivileges`, `PrivateTmp`) اجرا می‌شن. `setup_downloader_bot.sh`
مسیرها رو خودکار با `INSTALL_DIR` هماهنگ می‌کنه.

نصب دستی (اگه setup رو اجرا نکردی):

```bash
sudo sed 's|/opt/downloader_bot|/srv/downloader_bot|g' \
  deploy/systemd/downloader-bot.service | sudo tee /etc/systemd/system/downloader-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now downloader-bot
```

---

## 🔑 نکته‌ی Local Bot API Server

برای فایل‌های تا **۲ گیگ** (به‌جای سقف ۵۰ مگابایتی Bot API معمولی)،
کانتینر `telegram-bot-api` لازمه. جفت `api_id`/`api_hash` پیش‌فرض،
جفتِ منتشرشده‌ی عمومی **Telegram Desktop** هست که همه‌جای اینترنت پخشه و
کار می‌کنه — نیازی به ساخت اپ شخصی تو my.telegram.org نیست.
از `.env` (`TELEGRAM_API_ID` / `TELEGRAM_API_HASH`) قابل بازنویسیه.

---

## 🩺 بعد از نصب

```bash
sudo systemctl status downloader-bot      # وضعیت
sudo journalctl -u downloader-bot -f      # لاگ زنده
docker logs telegram-bot-api --tail 20    # لاگ Local Bot API
tail -f /opt/downloader_bot/logs/run_bot.log
```

## 🔄 آپدیت کد

```bash
cd /opt/downloader_bot
git pull
venv/bin/alembic upgrade head             # فقط اگه فیلد جدید تو دیتابیس اضافه شده
sudo systemctl restart downloader-bot
```

یا صرفاً `sudo bash deploy/deploy.sh <repo-url>` رو دوباره بزن (pull می‌کنه).

---

📖 مستندات کامل‌تر: [`../docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md)