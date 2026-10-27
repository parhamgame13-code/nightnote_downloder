# 🚀 راهنمای استقرار (Deployment)

> ⚡️ **سرور جدید از صفر؟ مسیر کوتاه (تک‌دستور):**
>
> ```bash
> # فقط همین یک خط — خودش پروژه رو دانلود، پیش‌نیازها رو نصب،
> # .env رو تعاملی پر و همه‌چیز رو راه‌اندازی می‌کنه:
> curl -fsSL https://raw.githubusercontent.com/USER/REPO/main/deploy/deploy.sh | sudo bash -s -- https://github.com/USER/REPO.git
> ```
>
> یا اگه پروژه از قبل رو سروره:
> ```bash
> cd /opt/downloader_bot
> sudo bash deploy/deploy.sh https://github.com/USER/REPO.git
> ```
>
> **یا فقط نصب (پروژه از قبل هست):**
> ```bash
> sudo bash deploy/setup_downloader_bot.sh
> ```
> ← کاربر سرویس (غیر-root)، Docker + کانتینر Local Bot API، venv،
> requirements، migration ها و سرویس systemd رو خودکار انجام می‌ده.
>
> منبعِ اصلی‌یِ استقرار:
> `deploy/setup_downloader_bot.sh` (نصب/تعمیر idempotent)،
> `deploy/deploy.sh` (دانلود + تعاملی)، و
> `deploy/systemd/downloader-bot.service` + `downloader-bot-directlink.service`
> (کاربر غیر-root + hardening با ProtectSystem).
> بقیه‌ی این سند، روش‌های دستی و جایگزین‌ان (برای فهم عمیق‌تر یا استقرارهای خاص).

## 1️⃣ استقرار محلی (Local - Linux)

### الف) با systemd

#### 1. فایل service ایجاد کنید:
```bash
sudo nano /etc/systemd/system/downloader-bot.service
```

#### 2. محتوا:
```ini
[Unit]
Description=Downloader Bot Service
After=network-online.target redis.service
Wants=network-online.target

[Service]
Type=simple
User=downloader
WorkingDirectory=/home/downloader/downloader_bot
ExecStart=/home/downloader/downloader_bot/venv/bin/python -m bot.main
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

#### 3. فعال کنید:
```bash
sudo systemctl daemon-reload
sudo systemctl enable downloader-bot
sudo systemctl start downloader-bot
sudo systemctl status downloader-bot
```

#### 4. مراقبت:
```bash
# لاگ‌ها
sudo journalctl -u downloader-bot -f

# توقف/شروع
sudo systemctl stop downloader-bot
sudo systemctl restart downloader-bot
```

### ب) با Celery Worker (systemd)

#### Worker service:
```bash
sudo nano /etc/systemd/system/downloader-worker.service
```

```ini
[Unit]
Description=Downloader Celery Worker
After=network-online.target redis.service
Wants=network-online.target

[Service]
Type=simple
User=downloader
WorkingDirectory=/home/downloader/downloader_bot
ExecStart=/home/downloader/downloader_bot/venv/bin/celery -A worker.celery_app worker --loglevel=info -Q download,upload,default
Restart=always

[Install]
WantedBy=multi-user.target
```

#### Beat service (scheduler):
```bash
sudo nano /etc/systemd/system/downloader-beat.service
```

```ini
[Unit]
Description=Downloader Celery Beat
After=network-online.target redis.service

[Service]
Type=simple
User=downloader
WorkingDirectory=/home/downloader/downloader_bot
ExecStart=/home/downloader/downloader_bot/venv/bin/celery -A worker.celery_app beat --loglevel=info
Restart=always

[Install]
WantedBy=multi-user.target
```

---

## 2️⃣ استقرار با Docker

### الف) فایل Dockerfile:
```dockerfile
FROM python:3.11-slim

WORKDIR /app

# نصب وابستگی‌های سیستم
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# نصب Deno (yt-dlp برای دور زدن چالش‌های جاوااسکریپت یوتیوب بهش نیاز داره)
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

# نصب وابستگی‌های Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# کپی کد
COPY . .

# پوشه‌ها
RUN mkdir -p logs temp

# اجرا
CMD ["python", "-m", "bot.main"]
```

### ب) اجرای با Docker:
```bash
# ساخت image
docker build -t downloader-bot .

# اجرای container
docker run -d \
  --name downloader-bot \
  --restart always \
  -e BOT_TOKEN="your_token" \
  -e REDIS_URL="redis://redis:6379/0" \
  -e DATABASE_URL="postgresql+asyncpg://user:pass@postgres/db" \
  -v logs:/app/logs \
  -v temp:/tmp/downloader_bot \
  --network downloader_network \
  downloader-bot
```

### ج) Docker Compose:
```bash
# Redis + PostgreSQL
docker-compose up -d

# بات
docker-compose run -d downloader-bot python -m bot.main

# Worker
docker-compose run -d downloader-worker celery -A worker.celery_app worker -Q download,upload,default

# Logs
docker-compose logs -f downloader-bot
```

---

## 3️⃣ استقرار در VPS

### الف) تهیه VPS:
1. Ubuntu 22.04 LTS یا جدیدتر
2. حداقل 2 GB RAM
3. حداقل 50 GB Storage

### ب) تنظیم اولیه:
```bash
# آپدیت
sudo apt update && sudo apt upgrade -y

# نصب Python
sudo apt install -y python3.11 python3.11-venv python3-pip

# نصب Redis
sudo apt install -y redis-server
sudo systemctl enable redis-server
sudo systemctl start redis-server

# نصب PostgreSQL (اختیاری)
sudo apt install -y postgresql postgresql-contrib
sudo systemctl enable postgresql

# نصب ffmpeg (ضروریه - بدونش نه ویدیو دانلود میشه نه تامبنیل ساخته میشه)
sudo apt install -y ffmpeg

# نصب Deno (yt-dlp برای دور زدن چالش‌های جاوااسکریپت یوتیوب بهش نیاز داره)
curl -fsSL https://deno.land/install.sh | sudo DENO_INSTALL=/usr/local sh
deno --version
```

### ج) پیکربندی:
```bash
# کاربر جدید
sudo useradd -m -d /home/downloader -s /bin/bash downloader
sudo su - downloader

# Clone پروژه
git clone <repo>
cd downloader_bot

# محیط
python3.11 -m venv venv
source venv/bin/activate

# وابستگی‌ها
pip install -r requirements.txt

# تنظیمات
cp .env.example .env
nano .env  # تکمیل کنید
```

### د) systemd:
```bash
exit  # برگشت به root
sudo cp /home/downloader/downloader_bot/downloader-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable downloader-bot
sudo systemctl start downloader-bot
```

---

## 4️⃣ استقرار در Heroku

### الف) Procfile:
```
web: python -m bot.main
worker: celery -A worker.celery_app worker -Q download,upload,default
beat: celery -A worker.celery_app beat
```

### ب) استقرار:
```bash
# دپلوی
git push heroku main

# مشاهده لاگ‌ها
heroku logs -t
```

---

## 5️⃣ استقرار در AWS

### الف) EC2:
1. Ubuntu AMI انتخاب کنید
2. t3.medium یا بالاتر
3. بقیه مانند VPS

### ب) RDS برای PostgreSQL:
```bash
# در .env
DATABASE_URL=postgresql+asyncpg://user:pass@rds-endpoint/db
```

### ج) ElastiCache برای Redis:
```bash
REDIS_URL=redis://elasticache-endpoint:6379/0
```

---

## 🔒 نکات امنیتی

### الف) Firewall:
```bash
# فقط SSH و HTTP
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

### ب) .env:
```bash
# حقوق دسترسی
chmod 600 .env
```

### ج) SSL:
```bash
# اگه API endpoint لازم باشد
sudo apt install -y certbot
```

---

## 📊 مانیتورینگ

### الف) بررسی وضعیت:
```bash
# سرویس
sudo systemctl status downloader-bot

# استفاده منابع
top
htop

# Redis
redis-cli info
```

### ب) Logging:
```bash
# لاگ‌های سیستم
sudo journalctl -u downloader-bot -f -n 100

# لاگ‌های App
tail -f logs/bot.log
```

### ج) Backup:
```bash
# دیتابیس
pg_dump -U downloader downloader_bot > backup.sql

# فایل‌ها
tar -czf backup.tar.gz bot.db logs/
```

---

## 🚨 بازیابی از مشکل

### بات متوقف شد:
```bash
sudo systemctl restart downloader-bot
sudo journalctl -u downloader-bot -f
```

### دیتابیس خراب:
```bash
# Backup را restore کنید
psql -U downloader downloader_bot < backup.sql
```

### Redis نیست:
```bash
sudo systemctl restart redis-server
```

---

## ✅ لیست بررسی استقرار

- [ ] Python 3.11+ نصب شده
- [ ] وابستگی‌ها نصب شدند
- [ ] .env تکمیل شد
- [ ] Redis اجرا می‌شود
- [ ] دیتابیس تهیه شد
- [ ] systemd service ایجاد شد
- [ ] بات شروع می‌شود بدون خطا
- [ ] لاگ‌ها طبیعی‌اند
- [ ] Celery worker اجرا می‌شود
- [ ] یک دانلود آزمایش کنید

---

سوالات؟ GitHub Issues را بررسی کنید!

## 🆕 راه‌اندازی روی سرور جدید (چک‌لیست کامل — تجربه‌ی واقعی دیپلوی)

این بخش خلاصه‌ی مشکلاتیه که موقع راه‌اندازی روی یه سرور تازه (بدون هیچ سابقه‌ای) پیش اومد و راه‌حلشون.

### 1. مسیر فایل سرویس و venv
اگه پروژه رو زیر `/root/downloader_bot` اکسترکت کردی (نه `/home/downloader/...`)، حتماً مسیرهای `WorkingDirectory` و `ExecStart` توی `downloader-bot.service` رو با مسیر واقعی هماهنگ کن. اگه venv داری، `ExecStart` باید به پایتونِ داخل venv اشاره کنه، نه `/usr/bin/python3`:
```ini
ExecStart=/root/downloader_bot/venv/bin/python3 /root/downloader_bot/run.py
```

### 2. نصب کامل dependencyها
بعد از ساخت venv، همیشه full requirements رو نصب کن:
```bash
/root/downloader_bot/venv/bin/pip install -r /root/downloader_bot/requirements.txt
```
⚠️ نکته: `redis<8.0` به `async_timeout` نیاز داره ولی خودش به‌عنوان dependency نمیارتش؛ اگه بعد از نصب requirements هنوز خطای `ModuleNotFoundError: No module named 'async_timeout'` گرفتی، دستی نصبش کن:
```bash
/root/downloader_bot/venv/bin/pip install async_timeout
```

### 3. Local Bot API Server (پورت 8081)

خبر خوب: از این به بعد سرویس `telegram-bot-api` **جزو `docker-compose.yml` خود پروژه‌ست** و با
`docker compose up -d telegram-bot-api` خودکار بالا میاد (`run.sh` هم خودش این کار رو می‌کنه).
کلیدهای `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` پیش‌فرضِ عمومی دارن (جفت منتشرشده‌ی Telegram Desktop
تو سورس باز تلگرام) و تو `.env` قابل بازنویسی‌ان.

راه دستی (فقط اگه کانتینر مشترکِ جدا از پروژه می‌خوای - مثلاً برای چند بات با ریپوهای جدا):

اول Docker رو نصب کن (اگه نیست):
```bash
curl -fsSL https://get.docker.com | sudo sh
sudo systemctl enable --now docker
```

سپس با API ID/Hash که از https://my.telegram.org گرفتی (اگه دفعه‌ی اول لاگین کنی، برنامه‌ی قبلی که ساخته بودی رو نشونت می‌ده، پس گم نمی‌شه)، کانتینر رو بساز:
```bash
sudo mkdir -p /var/lib/telegram-bot-api

sudo docker run -d \
  --name telegram-bot-api \
  --restart always \
  -p 8081:8081 \
  -v /var/lib/telegram-bot-api:/var/lib/telegram-bot-api \
  -e TELEGRAM_API_ID=17349 \
  -e TELEGRAM_API_HASH=344583e45741c457fe1862106095a5eb \
  -e TELEGRAM_LOCAL=1 \
  aiogram/telegram-bot-api:latest
```
⚠️ توجه: این ایمیج (`aiogram/telegram-bot-api`) مقادیر رو از **environment variable** می‌خونه (`TELEGRAM_API_ID` / `TELEGRAM_API_HASH`)، نه از فلگ‌های CLI مثل `--api-id`.

چک کن که کانتینر پایدار مونده (نه در حال restart loop):
```bash
docker ps | grep telegram-bot-api
docker logs telegram-bot-api --tail 20
```

### 4. ترتیب پیشنهادی برای دیپلوی روی سرور تازه
1. اکسترکت پروژه + ساخت venv + `pip install -r requirements.txt`
2. نصب و بالا آوردن Docker + کانتینر `telegram-bot-api` (بند ۳ بالا)
3. کپی و اصلاح `downloader-bot.service` (مسیرهای درست)
4. `systemctl daemon-reload && enable && start`
5. چک با `systemctl status` + `tail -n 50 logs/run_bot.log`

## 🆕 آپدیت کد روی سرور (بعد از هر تغییر)

چون پروژه الان `alembic` فعال داره، بعد از هر آپدیت کدی که فیلد جدید به
دیتابیس اضافه کرده باشه (مثل تنظیمات کاربر یا `raw_domain`)، این دو قدم
کافیه - نیازی به `ALTER TABLE` دستی نیست:

```bash
cd /root/downloader_bot
venv/bin/alembic upgrade head
sudo systemctl restart downloader-bot
```

اگه فقط کد عوض شده (بدون فیلد جدید تو دیتابیس)، همون `systemctl restart`
به‌تنهایی کافیه.

### دستورات جدید ادمین (بعد از ری‌استارت فعالن)
| دستور | کاربرد |
|-------|--------|
| `/topsites` | پرکاربردترین سایت‌ها + دامنه‌هایی که «unknown» تشخیص داده شدن |
| `/userinfo <telegram_id>` | breakdown سایت‌های یه کاربر خاص (یا ریپلای رو پیام فوروارد شده‌ش) |
| `/broadcast` | پیام همگانی - متن رو می‌فرستی، بعد انتخاب می‌کنی «همه» یا «فعال‌های ۷ روز اخیر» |
| `/cancel` | لغو هر حالت انتظاریِ در جریان (broadcast، رنیم، thumbnail، timezone) |

### Docker Compose (روش جایگزین systemd، اختیاری)
از این به بعد `docker-compose.yml` سرویس‌های `bot`/`worker`/`beat` رو هم
داره (قبلاً فقط redis/postgres بود). اگه یه روز خواستی از systemd به
Docker Compose سوییچ کنی:
```bash
docker compose up -d --build
```
سرویس `telegram-bot-api` هم پیش‌فرض جزو compose هست (پورت فقط روی localhost هاست؛
کلیدهای api_id/api_hash پیش‌فرض عمومی دارن و از `.env` قابل بازنویسی‌ان).
اگه از قبل کانتینر مشترک Local Bot API روی همین سرور داری، فقط سرویس
`telegram-bot-api` رو تو compose کامنت کن که با پورت 8081 تداخل نکنه -
بات‌ها از همون مشترک استفاده می‌کنن.
