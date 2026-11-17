"""
اپ FastAPI نسخه‌ی وب.

نکته‌های مهم:
- API روت‌ها قبل از mount شدن فایل‌های استاتیک ثبت می‌شن تا /api/* و /dl/*
  و /docs همیشه به API برسن، نه به فایل استاتیک.
- هویت وب با میدلور کوکی ست می‌شه: اگه مرورگر کوکی nn_web_id معتبر نداشت،
  یه شناسه‌ی تازه ساخته و هم روی request.state می‌ذاره هم تو کوکی پاسخ.
- فقط یه worker باید وب رو اجرا کنه (پیشرفت/صف دانلود درون‌حافظه‌ست)؛
  uvicorn با workers=1 اجرا می‌شه (webapp/server.py).
"""
import asyncio
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from loguru import logger

from bot.config import settings
from db.session import close_db, init_db
from webapp import storage
from webapp.deps import WEB_COOKIE_NAME, is_valid_web_id, new_web_id
from webapp.routes_admin import router as admin_router
from webapp.routes_public import router as public_router

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _cors_origins() -> list[str]:
    raw = (settings.CORS_ORIGINS or "").strip()
    if not raw:
        return []
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


async def _cleanup_loop() -> None:
    """پاک‌سازی دوره‌ای پوشه‌های منقضیِ لینک وب (هر ساعت)."""
    while True:
        await asyncio.sleep(3600)
        try:
            storage.cleanup_expired()
        except Exception as e:
            logger.warning(f"پاک‌سازی دوره‌ای وب ناموفق بود: {e}")


def create_app() -> FastAPI:
    app = FastAPI(
        title="NightNote Downloader — نسخه‌ی وب",
        description="دانلودر وب: لینک بفرست، فایل رو بگیر. همون قابلیت‌های بات تلگرام، روی مرورگر.",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/api/openapi.json",
    )

    origins = _cors_origins()
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def _web_identity_cookie(request: Request, call_next):
        """ست کردن کوکی هویت وب اگه مرورگر نداشت (برای همه‌ی پاسخ‌ها)."""
        existing = request.cookies.get(WEB_COOKIE_NAME)
        web_id = existing if is_valid_web_id(existing) else new_web_id()
        request.state.nn_web_id = web_id
        response = await call_next(request)
        if not is_valid_web_id(existing):
            samesite = (settings.WEB_COOKIE_SAMESITE or "lax").lower()
            if samesite not in ("lax", "strict", "none"):
                samesite = "lax"
            response.set_cookie(
                WEB_COOKIE_NAME,
                web_id,
                max_age=365 * 24 * 3600,
                httponly=True,
                samesite=samesite,
                secure=(samesite == "none" or request.url.scheme == "https"),
                path="/",
            )
        return response

    @app.on_event("startup")
    async def _startup() -> None:
        Path(settings.TEMP_DIR).mkdir(parents=True, exist_ok=True)
        storage.base_dir()
        await init_db()
        logger.info("🌐 وب‌اپ آماده شد (دیتابیس + پوشه‌های فایل)")
        app.state.cleanup_task = asyncio.create_task(_cleanup_loop())

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        task = getattr(app.state, "cleanup_task", None)
        if task:
            task.cancel()
        await close_db()

    app.include_router(public_router)
    app.include_router(admin_router)

    # فایل‌های استاتیک (رابط کاربری) — حتماً آخر mount بشه
    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    else:
        logger.warning(f"پوشه‌ی استاتیک وب پیدا نشد: {STATIC_DIR}")

    return app


# برای ابزارهایی که دنبال app در سطح ماژول می‌گردن (مثلاً `uvicorn webapp.app:app`)
app = create_app()
