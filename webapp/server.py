"""
نقطه‌ی ورود وب‌سرور نسخه‌ی وب.

اجرا:
    python -m webapp.server

روی 0.0.0.0:$PORT گوش می‌ده (پیش‌فرض 8000) تا روی Render/Railway/Fly.io و
پشت reverse-proxy هم کار کنه. فقط یه worker اجرا می‌شه چون پیشرفت و صف
دانلودهای وب درون‌حافظه‌ی همین پروسه‌ست.
"""
import os

import uvicorn


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(
        "webapp.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=port,
        workers=1,
        log_level=os.environ.get("UVICORN_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
