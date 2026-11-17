# 🌐 نسخه‌ی وب NightNote — راهنمای معماری و دپلوی

نسخه‌ی وب، همون دانلودر بات تلگرام رو به‌صورت سایت در اختیار کاربر
می‌ذاره: لینک رو می‌چسبونه، کیفیت رو انتخاب می‌کنه، و فایل رو مستقیم
دانلود می‌کنه — بدون نیاز به تلگرام. وب و بات **همون دیتابیس** رو
می‌بینن (محدودیت روزانه و بن‌ها مشترک کار می‌کنن).

## معماری

```
┌────────────────────────────────────────────────────────────────┐
│  مرورگر (رابط وب: webapp/static — HTML/CSS/JS، بدون build)     │
└───────────────────────────────┬────────────────────────────────┘
                                │ HTTP (same-origin یا پروکسی)
┌───────────────────────────────▼────────────────────────────────┐
│  FastAPI (webapp/) — روی $PORT                                 │
│  ├─ routes_public: inspect / jobs / history / settings / /dl/ │
│  ├─ routes_admin:  stats / users / toggle / broadcast / texts  │
│  ├─ downloader:    رانرِ async وب (بدون Celery)                │
│  │   ├─ yt-dlp (subprocess) — همون quality map بات             │
│  │   ├─ utils/* — همون ماژول‌های بات (Spotify/Instagram/…)     │
│  │   └─ ffmpeg — HLS / تبدیل / video_note                      │
│  └─ storage: فایل‌ها زیر DIRECT_LINK_DIR/<token>/<filename>    │
└───────┬────────────────────────────────────────────┬───────────┘
        │  همان دیتابیس (SQLAlchemy async)           │ Redis (فقط بات)
        ▼                                            ▼
┌───────────────────────────┐            ┌──────────────────────┐
│  User / DownloadJob /     │            │  Celery worker+beat  │
│  DownloadStats / BotConfig│            │  (بات تلگرام)        │
│  (وب و بات مشترک)         │            └──────────┬───────────┘
└───────────────────────────┘                       │
                                         ┌──────────▼───────────┐
                                         │  بات تلگرام (polling)│
                                         └──────────────────────┘
```

### هویت کاربران وب

کاربر وب با کوکی `nn_web_id` (uuid) شناخته می‌شه. از روی uuid یه
`telegram_id` **منفی و قطعی (deterministic)** ساخته می‌شه و همون سطر
`User` بات براش ساخته/بازیابی می‌شه. نتیجه:

- وب و بات جدول‌های `User` و `DownloadJob` رو **اشتراک** می‌گیرن؛
  محدودیت روزانه و بن برای هر دو یکی کار می‌کنه.
- id منفی = کاربر وب (idهای تلگرام همیشه مثبت‌ان)؛ پنل ادمین و
  broadcast به‌راحتی این‌ها رو از هم جدا می‌کنن.
- جاب‌های وب `chat_id=0` و `message_id=None` دارن.
- **دایرکت‌لینک:** جاب‌های وب همیشه فایل سرو‌شده دارن و به
  `DIRECT_LINK_ENABLED` وابسته نیستن (اون سوییچ فقط مال باته)؛ با این
  حال فیلدهای direct-link جاب (`set_direct_link`) مثل بات پر می‌شه.
- **کش:** سطرهای `FileCache` فقط `telegram_file_id` دارن و برای وب
  قابل استفاده نیستن؛ وب فایل‌های محلی رو با کلید
  `get_cache_hash(url, quality, file_type)` زیر
  `DIRECT_LINK_DIR/.webcache/` کش می‌کنه (جدا از کش تلگرام).

### نگاشت تنظیمات بات → وب

| فیلد User | رفتار در بات | رفتار در وب |
|---|---|---|
| `spoiler_effect` | مدیا با افکت spoiler | تامبنیل/پیش‌نمایش عکس تار (blur) می‌شه |
| `upload_as_document` | ارسال به‌صورت فایل | پلیر/پیش‌نمایش نشون داده نمی‌شه، فقط دکمه‌ی دانلود |
| `timezone` | زمان‌بندی پیام‌ها | قالب‌بندی تاریخ‌ها در تاریخچه |
| `show_rename_option` | دکمه‌ی تغییر نام | فیلد تغییر نام زیر لینک نشون داده می‌شه |
| `receive_screenshots` | دریافت اسکرین‌شات | تامبنیل‌ها در کارت بررسی لینک نشون داده می‌شن |
| `custom_thumbnail_file_id` | تامبنیل دلخواه تلگرام | در وب استفاده نمی‌شه (file_id تلگرام قابل سرو نیست) |

## جدول تطابق بات ↔ وب

| قابلیت | بات | وب | توضیح |
|---|---|---|---|
| دانلود یوتیوب/اینستاگرام/تیک‌تاک/توییتر/پینترست/ساندکلاد | ✅ | ✅ | همون utils و yt-dlp |
| اسپاتیفای (ترک/آلبوم/پلی‌لیست) | ✅ | ✅ | جاب‌های فرزند با `playlist_id` مشترک |
| انتخاب کیفیت (best/1080/720/480/audio) | ✅ | ✅ | همون quality map |
| video_note | ✅ | ⚠️ | دانلود می‌شه (mp4 مربعی)، ولی «پیام ویدیویی» تلگرام نداره |
| فایل‌های عمومی / HLS / APK / Gofile / فضای ابری | ✅ | ✅ | همون resolverها |
| آپلود فایل و گرفتن لینک مستقیم | ✅ | ✅ | `/api/upload`، سقف `MAX_FILE_SIZE_MB` |
| تغییر نام فایل | ✅ | ✅ | فیلد rename (با `show_rename_option`) |
| محدودیت روزانه | ✅ | ✅ | دیتابیس مشترک |
| لینک مستقیم با انقضا + شمارش دانلود | ✅ | ✅ | `/dl/<token>/<filename>`، پشتیبانی Range |
| تاریخچه | ✅ | ✅ | `/api/history` |
| پنل ادمین (آمار، بن، toggle، broadcast، متن‌ها) | ✅ | ✅ | با رمز `ADMIN_PASSWORD` |
| گروه‌ها / ریپلای / ری‌اکشن | ✅ | ❌ | مخصوص تلگرام — در وب معادلی نداره |
| force-join (عضویت اجباری کانال) | ✅ | ❌ | مخصوص تلگرام — در وب اعمال نمی‌شه |
| self-destruct (حذف خودکار پیام در تلگرام) | ✅ | ❌ | در وب فایل‌ها با انقضای لینک پاک می‌شن |
| دانلود موازی با Celery | ✅ | ⚠️ | وب رانرِ async خودش رو داره؛ سقف با `WEB_MAX_CONCURRENT_DOWNLOADS` |

## متغیرهای محیطی جدید

| متغیر | پیش‌فرض | توضیح |
|---|---|---|
| `ADMIN_PASSWORD` | `""` (خالی) | رمز پنل ادمین وب — خالی = پنل غیرفعال |
| `WEB_SECRET_KEY` | `""` | کلید امضای کوکی سشن ادمین؛ خالی = کلید موقتِ هر استارت (سشن‌ها بعد ری‌استارت باطل می‌شن) |
| `WEB_PUBLIC_URL` | `""` | آدرس عمومی سایت برای ساخت لینک دانلود؛ خالی = آدرس درخواستِ رسیده |
| `CORS_ORIGINS` | `""` | مبداهای مجاز CORS (کاما جدا)؛ خالی = فقط same-origin |
| `WEB_MAX_CONCURRENT_DOWNLOADS` | `2` | سقف دانلودهای همزمان وب |
| `WEB_RATE_LIMIT_PER_MINUTE` | `60` | محدودیت نرخ API وب به‌ازای هر IP؛ `0` = غیرفعال |
| `WEB_INSPECT_TIMEOUT` | `30` | تایم‌اوت استخراج متادیتا (ثانیه) |
| `WEB_COOKIE_SAMESITE` | `lax` | SameSite کوکی هویت وب؛ cross-origin باید `none` + HTTPS باشه |
| `RUN_WEB` / `RUN_BOT` / `RUN_WORKER` / `RUN_BEAT` / `RUN_REDIS` | همه `1` | فلگ‌های `run_all.py`؛ `RUN_BOT` بدون `BOT_TOKEN` خودکار رد می‌شه |
| `PORT` | `8000` | پورت وب (پلتفرم‌های PaaS خودشون ست می‌کنن) |
| `WEB_PORT` | `8000` | پورت وب روی هاست در docker-compose |
| `API_BASE_URL` | — (فقط Cloudzyro) | آدرس بک‌اند برای پروکسی Cloudzyro (اجباری در اون دپلوی) |

متغیرهای قدیمی (مثل `BOT_TOKEN`، `DATABASE_URL`، `REDIS_URL`،
`MAX_FILE_SIZE_MB`، `INSTAGRAM_COOKIE_FILE`) بدون تغییر کار می‌کنن و تو
`.env.example` هستن.

## اجرای لوکال

```sh
pip install -r requirements.txt
cp .env.example .env   # و BOT_TOKEN و ADMIN_PASSWORD رو پر کن
python run_all.py      # وب + بات + ورکر + beat
# یا فقط وب:
RUN_BOT=0 RUN_WORKER=0 RUN_BEAT=0 python -m webapp.server
```

سایت روی `http://localhost:8000` بالا میاد. برای تست سریع بدون بات،
`BOT_TOKEN` رو خالی بذار — `run_all.py` خودش بات رو رد می‌کنه.

## دپلوی روی VPS (Docker Compose + TLS)

1. ریپو رو کلون کن و `.env` رو بساز (`BOT_TOKEN`، `ADMIN_IDS`،
   `ADMIN_PASSWORD`، `WEB_SECRET_KEY`، و `WEB_PUBLIC_URL` با آدرس دامنه).
2. سرویس وب رو بالا بیار:
   ```sh
   docker compose up -d --build
   ```
   سرویس `web` روی `${WEB_PORT:-8000}` بالا میاد؛ بات و ورکر و beat هم
   سرویس‌های جدا هستن.
3. یه reverse proxy (Caddy یا nginx) با TLS جلوی پورت وب بذار و
   `WEB_PUBLIC_URL` رو با آدرس `https://` دامنه ست کن. مثال Caddy:
   ```
   nightnote.example.com {
       reverse_proxy localhost:8000
   }
   ```
4. **فقط یه بات‌پولر:** هیچ‌وقت دو جا (مثلاً هم VPS هم Render) با یه
   `BOT_TOKEN` بات رو بالا نیار — تلگرام فقط یه polling رو قبول می‌کنه
   و دومی با `Terminated by other getUpdates` می‌میره.

## دپلوی روی Render (Blueprint)

1. تو Render: New → Blueprint → همین ریپو. `render.yaml` خونده می‌شه.
2. مقدارهای `BOT_TOKEN` و `ADMIN_IDS` رو تو صفحه‌ی Environment پر کن؛
   `ADMIN_PASSWORD` و `WEB_SECRET_KEY` خودکار ساخته می‌شن (بعداً از
   همون صفحه می‌تونی ببینیشون/عوضشون کنی).
3. بعد از اولین دپلوی، `WEB_PUBLIC_URL` رو با آدرس
   `https://<name>.onrender.com` ست کن و ریدپلوی کن.

⚠️ **هشدار پلن free:** سرویس بعد از ~۱۵ دقیقه بیکاری می‌خوابه و **بات
تلگرام هم باهاش می‌خوابه**. برای بات همیشه‌روشن باید پلن پولی بگیری.
⚠️ **دیسک موقتی:** روی free، دیتابیس SQLite و فایل‌های دانلودشده با هر
ری‌استارت/ریدپلوی پاک می‌شن. راه‌حل: دیسک دائمی (فقط پلن پولی — بلاک
کامنت‌شده تو `render.yaml`) یا Postgres خارجی (Render Postgres) با
`DATABASE_URL` مناسب.

## دپلوی روی Railway

1. New Project → Deploy from GitHub repo → همین ریپو.
2. Railway خودش `Dockerfile` رو تشخیص می‌ده و با `run_all.py` همه‌چیز
   رو بالا میاره (پورت رو خودش از `$PORT` می‌گیره).
3. متغیرها رو ست کن: `BOT_TOKEN`، `ADMIN_IDS`، `ADMIN_PASSWORD`،
   `WEB_SECRET_KEY`، و بعد از دپلوی `WEB_PUBLIC_URL` با آدرس Railway.
4. اگه می‌خوای داده‌ها بمونن: یه Postgres به پروژه اضافه کن و
   `DATABASE_URL` رو به اون وصل کن (یا Volume بساز و
   `DATABASE_URL=sqlite+aiosqlite:////data/bot.db` بذار).
5. Railway `railway.toml` جدا نداره — همه‌چیز از Dockerfile و متغیرهای
   محیطی میاد.

## دپلوی روی Fly.io

```sh
fly auth login
# اسم اپ رو تو fly.toml عوض کن (باید یکتا باشه)
fly secrets set BOT_TOKEN="..." ADMIN_IDS="..." ADMIN_PASSWORD="..."
fly secrets set WEB_SECRET_KEY="$(openssl rand -hex 32)"
fly deploy
```

`auto_stop_machines=false` تو `fly.toml` مهمه تا ماشین با بیکاری متوقف
نشه و بات بیدار بمونه. برای دیسک دائمی، دستورهای کامنت‌شده تو `fly.toml`
رو ببین (`fly volumes create` + `DATABASE_URL` روی `/data`).

## دپلوی روی Cloudzyro (فقط رابط وب + پروکسی)

Cloudzyro پایتون اجرا نمی‌کنه، پس **اول** بک‌اند رو روی یکی از
گزینه‌های بالا دپلوی کن، **بعد** پوشه‌ی `cloudzyro/` رو به‌عنوان سایت
Node.js روی Cloudzyro دپلوی کن و `API_BASE_URL` رو با آدرس بک‌اند ست
کن. راهنمای کامل: `cloudzyro/README.md` (شامل روش استاتیک خالص با
`config.js` + `CORS_ORIGINS`).

## رمز ادمین

پنل ادمین وب (`/admin` تو رابط وب) با `ADMIN_PASSWORD` محافظت می‌شه.
اگه خالی باشه، پنل کاملاً غیرفعاله (حتی صفحه‌ی لاگین هم 503 می‌ده).
سشن ادمین یه کوکی HMAC-امضاشده با عمر ۱۲ ساعت هست. `ADMIN_IDS` بات
ربطی به پنل وب نداره.

## کوکی‌های اینستاگرام

مکانیزم همون مکانیزم باته و تغییری نکرده: فایل کوکی با
`INSTAGRAM_COOKIE_FILE` (پیش‌فرض `cookies/instagram_cookies.txt`)، و
فقط فایل کوکی‌ای که خودت ساختی رو بذار — هیچ کوکی‌ای تو ریپو نیست و
نباید باشه. وب هم از همون مسیر کوکی استفاده می‌کنه.

## عیب‌یابی

| مشکل | علت محتمل / راه‌حل |
|---|---|
| یوتیوب 403 / «Sign in to confirm» | IP دیتاسنتری (Render/Railway/Fly) توسط یوتیوب محدود شده. راه‌حل‌ها: کوکی یوتیوب معتبر، یا دپلوی روی VPS با IP تمیز، یا صبر. رانر وب خودش با کلاینت‌های mweb/android/tv دوباره تلاش می‌کنه. |
| دیسک پر شد | فایل‌های دانلودشده زیر `DIRECT_LINK_DIR` و `TEMP_DIR` جمع می‌شن؛ cleanup ساعتیِ وب فایل‌های منقضی رو پاک می‌کنه، ولی حجم `MAX_FILE_SIZE_MB` رو هم چک کن و دیسک رو مانیتور کن. |
| Redis وصل نمی‌شه | `run_all.py` اگه `REDIS_URL` لوکال باشه و redis نصب باشه، خودش redis-server توکار بالا میاره. اگه Redis جدا داری، `REDIS_URL` رو درست ست کن و `RUN_REDIS=0` بذار. |
| بات با `Terminated by other getUpdates` می‌میره | دو پولر با یه توکن بالا هستن — فقط یه جا بات رو اجرا کن. |
| پنل ادمین 503 می‌ده | `ADMIN_PASSWORD` ست نشده. |
| رابط وب API رو پیدا نمی‌کنه | اگه رابط جدا سرو می‌شه، `config.js` یا فیلد «آدرس API» تو تنظیمات صفحه رو چک کن و `CORS_ORIGINS` بک‌اند رو ست کن. |
| دانلودها تو صف می‌مونن | `WEB_MAX_CONCURRENT_DOWNLOADS` (پیش‌فرض ۲) پره؛ صف وب جدا از صف Celery باته. |
| 401/403 روی `/dl/` | لینک منقضی شده یا توکن اشتباهه؛ از تاریخچه دوباره لینک بگیر. |
