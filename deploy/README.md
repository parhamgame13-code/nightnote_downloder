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

1. پیش‌نیازهای سیستم رو نصب می‌کنه: `git`, `python3-venv`, `curl`, `unzip`,
   `ffmpeg`, `redis-server` (+ فعال‌سازی Redis)
2. پروژه رو `git clone` می‌کنه (اگه از قبل با گیت هست `git pull` — زیپ هم قبوله)
3. **تعاملی** اطلاعات لازم رو می‌پرسه و **مستقیم داخل `.env` می‌نویسه**:
   - `BOT_TOKEN` (اجباری) — **با اعتبارسنجی واقعی**: همون لحظه با `getMe`
     چک می‌شه و یوزرنیم بات نشون داده می‌شه؛ توکن غلط همون‌جا رد می‌شه،
     نه آخرِ کار که بات کرش کرد
   - `ADMIN_IDS` (اجباری) — فقط عدد و کاما قبوله
   - `BOT_USERNAME` / `CHANNEL_USERNAME` / `ALLOWED_GROUP_IDS`
   - دیتابیس: SQLite (پیش‌فرض) یا PostgreSQL — اگه Postgres رو انتخاب کنی،
     خودش نصب می‌کنه، کاربر و دیتابیس می‌سازه و `DATABASE_URL` رو با رمز
     تصادفی تولید می‌کنه (هیچ کار دستی لازم نیست)
   - لینک مستقیم: روشن/خاموش + پورت + دامنه (با پرسش http/https — دیگه
     بدون SSL اجباراً https نمی‌شه)
4. **IP عمومی سرور رو خودکار تشخیص می‌ده** و اگه دامنه ندی،
   `DIRECT_LINK_PUBLIC_URL` رو `http://<IP>:<PORT>` می‌کنه
5. قبل از نصب، **خلاصه‌ی تنظیمات رو نشون می‌ده و تأیید می‌گیره**
6. `.env` رو می‌سازه (با `chmod 600`) — **بدون خراب کردن تنظیمات قبلی**:
   فقط کلیدهای خودش به‌روز می‌شن؛ کوکی‌ها، `FILELU_API_KEY` و هر ویرایش
   دستی دیگه حفظ می‌شن
7. اگه `ufw` فعال باشه پورت لینک مستقیم رو باز می‌کنه
8. `setup_downloader_bot.sh` رو صدا می‌زنه که Docker، Local Bot API،
   venv، migration و systemd رو نصب و سرویس رو استارت کنه

### اجرا

```bash
# از داخل ریپوی کلون‌شده:
sudo bash deploy/deploy.sh

# یا بدون کلون قبلی (مستقیم از گیت‌هاب):
curl -fsSL https://raw.githubusercontent.com/USER/REPO/main/deploy/deploy.sh \
  -o deploy.sh && sudo bash deploy.sh https://github.com/USER/REPO.git

# با زیپ:
sudo bash deploy/deploy.sh https://github.com/USER/REPO/archive/refs/heads/main.zip

# شاخه یا مسیر نصب دلخواه:
BRANCH=dev INSTALL_DIR=/opt/downloader_bot sudo bash deploy/deploy.sh
```

### غیرتعاملی (اتوماسیون / CI)

⚠️ `sudo` به‌صورت پیش‌فرض متغیرهای محیطی رو رد نمی‌کنه — از `sudo -E env` استفاده کن:

```bash
sudo -E env REPO_URL="https://github.com/USER/REPO.git" \
  BOT_TOKEN="123:ABC" ADMIN_IDS="123,456" \
  NONINTERACTIVE=1 bash deploy/deploy.sh
```

در حالت غیرتعاملی: `BOT_TOKEN` و `ADMIN_IDS` اجبارین؛ توکن نامعتبر (پاسخ
401 از تلگرام) کل دیپلوی رو همون اول متوقف می‌کنه.

> **idempotent** هست — اجرای دوباره = آپدیت کد با `git pull` + حفظ `.env`
> (فقط اگه مقدار جدیدی از env داده شده باشی همون کلید عوض می‌شه).
> دامنه‌ی پیش‌فرض مخزن خود پروژه‌ست؛ Enter بزنی همون کلون می‌شه.

---

## ۲️⃣ `setup_downloader_bot.sh` — فقط نصب (پروژه از قبل هست)

وقتی کد رو خودت گذاشتی سر جاش (git clone / scp / zip) و فقط می‌خوای
نصب/تعمیر بشه. کارهایی که می‌کنه:

- کاربر سیستمیِ بدون لاگین `downloader` (نه root!)
- پوشه‌های داده: `/var/lib/downloader_bot`, `/var/lib/telegram-bot-api`
- Docker + کانتینر Local Bot API Server (پورت 8081، فقط localhost)
- venv + نصب `requirements.txt` (با `sudo -u -H` که کش pip سالم بمونه)
- `alembic upgrade head` (migration ها)
- کپی و تنظیم unit های systemd از `deploy/systemd/`
- `systemctl enable` + `restart`

**فرقش با قبل:** اگه `.env` نباشه یا `BOT_TOKEN` خالی باشه، دیگه وسط کار
نمی‌ایسته که «برو با nano پرش کن» — خودش می‌پرسه (با همون اعتبارسنجی
توکن از تلگرام) و مستقیم داخل `.env` می‌نویسه. فقط وقتی ترمینال تعاملی
نداشته باشی (CI/pipe) با راهنمای واضح می‌ایسته.

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

یا صرفاً `sudo bash deploy/deploy.sh` رو دوباره بزن (pull می‌کنه و `.env`
قبلی رو حفظ می‌کنه — سوال‌ها با مقادیر فعلی `.env` پیش‌پر شدن؛ Enter بزنی
همون می‌مونه).

---

📖 مستندات کامل‌تر: [`../docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md)
