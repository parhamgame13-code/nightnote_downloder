"""
یه سرور aiohttp سبک و مستقل که فایل‌های زیر DIRECT_LINK_DIR رو با لینک
دانلود مستقیم (بدون نیاز به تلگرام) سرو می‌کنه.

پروسه‌ی جدا از خودِ بات/ورکره - چون هیچ ربطی به Telegram API نداره و نباید
با ترافیک دانلود کاربرها روی همون پورت/پروسه‌ی بات تداخل کنه.

اجرا:
    python -m webserver.direct_link_server

یا با systemd (فایل نمونه: downloader-bot-directlink.service) که خودکار
بالا میاد و اگه کرش کرد ری‌استارت می‌شه.

پشتِ سر یه ریورس‌پراکسی (nginx/caddy) با TLS بذارینش تا هم https داشته
باشین هم بشه چند تا دامنه/سرویس رو پشت یه IP سرو کرد؛ این سرور خودش فقط
HTTP ساده حرف می‌زنه.
"""
import mimetypes
from pathlib import Path

from aiohttp import web
from loguru import logger

from bot.config import settings

routes = web.RouteTableDef()

# aiohttp به‌صورت پیش‌فرض mp4/mkv رو نمی‌شناسه و بدون content-type درست
# می‌فرسته که پخش/دانلود مستقیم تو بعضی مرورگرها یا دانلود منیجرها رو خراب می‌کنه
mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("audio/mpeg", ".mp3")
mimetypes.add_type("audio/mp4", ".m4a")


@routes.get("/dl/{token}/{filename}")
async def handle_download(request: web.Request) -> web.StreamResponse:
    token = request.match_info["token"]
    filename = request.match_info["filename"]

    # جلوگیری از path traversal (../../etc/passwd و مشابه) - چه تو token چه تو filename
    if "/" in token or ".." in token or "/" in filename or ".." in filename:
        raise web.HTTPBadRequest(text="درخواست نامعتبر")

    base_dir = Path(settings.DIRECT_LINK_DIR).resolve()
    file_path = (base_dir / token / filename).resolve()

    try:
        file_path.relative_to(base_dir)
    except ValueError:
        raise web.HTTPForbidden(text="دسترسی غیرمجاز")

    if not file_path.is_file():
        raise web.HTTPNotFound(text="این لینک منقضی شده یا وجود نداره.")

    # ثبت بازدید - فقط برای درخواست‌های «شروع دانلود» (بدون Range یا Range از بایت
    # ۰)، نه برای هر تیکه‌ی جداگونه‌ی یه دانلود چندتکه (IDM و مدیرهای دانلود مشابه
    # هرکدوم چند کانکشن با Range مختلف باز می‌کنن که قبلاً هرکدوم جدا شمرده می‌شد
    # و عدد آمار رو چند برابر واقعی نشون می‌داد؛ پلیرها و پیش‌نمایش لینک هم اغلب
    # با یه Range جزئی یا HEAD می‌زنن که نباید «بازدید» حساب بشه)
    range_header = request.headers.get("Range", "")
    is_fresh_request = not range_header or range_header.strip().startswith("bytes=0-")
    if is_fresh_request:
        try:
            from db.session import async_session_maker
            from db.crud import DownloadJobCRUD

            async with async_session_maker() as session:
                await DownloadJobCRUD.increment_direct_link_hit(session, token)
        except Exception as e:
            logger.warning(f"افزایش شمارنده‌ی بازدید لینک {token} ناموفق بود: {e}")

    # FileResponse خودش Range request (دانلود قطعه‌قطعه/رزومه) رو هندل می‌کنه
    resp = web.FileResponse(file_path)
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@routes.get("/healthz")
async def healthz(request: web.Request) -> web.Response:
    return web.Response(text="ok")


def create_app() -> web.Application:
    Path(settings.DIRECT_LINK_DIR).mkdir(parents=True, exist_ok=True)
    app = web.Application(client_max_size=0)  # آپلود نداریم، فقط دانلود؛ سقف سایز درخواست مهم نیست
    app.add_routes(routes)
    return app


def main():
    logger.info(
        f"سرور لینک مستقیم روی پورت {settings.DIRECT_LINK_PORT} بالا میاد "
        f"(پوشه: {settings.DIRECT_LINK_DIR})"
    )
    web.run_app(create_app(), port=settings.DIRECT_LINK_PORT)


if __name__ == "__main__":
    main()
