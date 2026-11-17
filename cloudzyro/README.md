# NightNote روی Cloudzyro — رابط وب + پروکسی

> ⚠️ **واقعیت مهم:** Cloudzyro فقط سایت استاتیک و Node.js اجرا می‌کنه و
> پایتون روش کار نمی‌کنه. دانلودر واقعی (پایتون + yt-dlp + ffmpeg + بات
> تلگرام) **روی Cloudzyro اجرا نمی‌شه** و هیچ راهی برای این کار تو این
> پوشه وجود نداره. این پوشه فقط **رابط وب + پروکسی** هست: رابط وب رو سرو
> می‌کنه و درخواست‌های `/api/*` و `/dl/*` رو به بک‌اند پایتونی که جدا
> دپلوی شده پروکسی می‌کنه.

## راه‌اندازی (ترتیب مهمه)

### قدم ۱ — بک‌اند پایتون رو جدا دپلوی کن

اول بک‌اند رو روی یکی از این‌ها بالا بیار (راهنمای کامل: `docs/WEB_DEPLOYMENT.md`):

- **Render** — با `render.yaml` (Blueprint)
- **Railway** — Dockerfile به‌صورت خودکار تشخیص داده می‌شه
- **Fly.io** — با `fly.toml`
- **VPS خودت** — با docker compose و reverse proxy

آدرس عمومی بک‌اند رو بردار، مثلاً: `https://nightnote-downloader.onrender.com`

### قدم ۲ — سایت Cloudzyro رو از این پوشه بساز

1. تو Cloudzyro یه سایت جدید بساز و این ریپو رو وصل کن.
2. Root directory / مسیر دپلوی رو روی پوشه‌ی **`cloudzyro/`** بذار
   (این پوشه باید مستقل دپلوی بشه؛ `package.json` و `server.js` همین‌جا هستن).
3. نوع سایت: **Node.js** (با `npm start` که `node server.js` رو اجرا می‌کنه).
4. متغیر محیطی **`API_BASE_URL`** رو با آدرس بک‌اند قدم ۱ ست کن
   (بدون `/` آخر).
5. دپلوی کن. رابط وب روی آدرس Cloudzyro بالا میاد و همه‌ی درخواست‌های
   API از طریق پروکسی به بک‌اند می‌رسن — کاربر فقط یه آدرس می‌بینه.

## روش جایگزین: استاتیک خالص (بدون Node)

اگه می‌خوای فقط `public/` رو به‌صورت سایت استاتیک دپلوی کنی (بدون پروکسی):

1. تو `cloudzyro/public/config.js` آدرس بک‌اند رو بنویس:
   ```js
   window.__NN_API_BASE__ = "https://nightnote-downloader.onrender.com";
   ```
2. تو بک‌اند، متغیر `CORS_ORIGINS` رو با آدرس سایت Cloudzyro ست کن
   (مثلاً `https://nightnote.cloudzyro.app`) تا مرورگر اجازه‌ی
   درخواست cross-origin رو بده.
3. پوشه‌ی `public/` رو به‌عنوان سایت استاتیک دپلوی کن.

نکته: تو این حالت، کوکی هویت وب cross-origin می‌شه؛ برای کارکرد درست،
تو بک‌اند `WEB_COOKIE_SAMESITE=none` بذار (و سایت حتماً روی HTTPS باشه).

## به‌روزرسانی رابط وب

رابط وب تو `webapp/static/` توسعه داده می‌شه. بعد از هر تغییر، کپیِ این
پوشه رو با این دستور به‌روز کن (config.js دست‌نخورده می‌مونه):

```sh
sh cloudzyro/sync-static.sh
```

## فایل‌های این پوشه

| فایل | توضیح |
|---|---|
| `server.js` | سرور Node بدون هیچ وابستگی خارجی: سرو استاتیک + پروکسی `/api/*` و `/dl/*` با حفظ Range/Content-Disposition/استریم |
| `package.json` | Node 24، بدون dependency |
| `public/` | کپی رابط وب (index.html, app.js, styles.css, config.js) |
| `sync-static.sh` | همگام‌سازی از `webapp/static/` |
