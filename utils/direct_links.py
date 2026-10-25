"""
ابزار ساخت لینک دانلود مستقیم (به‌جای/علاوه‌بر آپلود در تلگرام).

فایل دانلودشده رو (با هاردلینک - یا کپی، اگه هاردلینک ممکن نبود، مثلاً چون
TEMP_DIR و DIRECT_LINK_DIR روی دو پارتیشن/دیسک جدا هستن) داخل یه پوشه‌ی
توکن‌دار زیر DIRECT_LINK_DIR می‌ذاره تا وب‌سرور جدا (webserver/direct_link_server.py)
بتونه سروش کنه. توکن تصادفی و غیرقابل‌حدسه، پس صرفِ داشتنِ لینک برای دسترسی کافیه
(هیچ auth دیگه‌ای لازم نیست) - مثل خیلی از سرویس‌های اشتراک فایل.

پاک‌سازی اسم‌ها تو utils/filename.py هست (مشترک با بقیه‌ی سناریوهای اسم فایل).
"""
import os
import shutil
import secrets
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from loguru import logger

from bot.config import settings
from utils.filename import sanitize_ascii_filename  # noqa: F401  (re-export برای سازگاری)


def _safe_filename(name: str) -> str:
    """اسم فایل رو برای استفاده تو URL/مسیر امن می‌کنه (بدون / یا کاراکترهای عجیب)"""
    name = os.path.basename(name)
    name = name.replace("/", "_").replace("\\", "_")
    return name or "file"


def _ascii_filename(job_id: int, original_name: str) -> str:
    """اسم فایلِ توی URL رو انگلیسی/ASCII می‌سازه (nightnote_123.mp4) - فال‌بکِ
    پیش‌فرض وقتی کاربر اسم دلخواهی نداده. اسم اصلی (فارسی) گم نمی‌شه، همون
    توی کپشن فایلی که تو تلگرام آپلود می‌شه هست؛ این فقط برای لینکِ سرور
    استفاده می‌شه که تمیزتر و قابل‌اشتراک‌گذاری‌تر باشه."""
    ext = Path(original_name).suffix or ""
    if not ext.isascii():
        ext = ""
    return f"nightnote_{job_id}{ext}"


def create_direct_link(
    job_id: int, file_path: Path, custom_filename: Optional[str] = None
) -> tuple[str, str] | None:
    """
    یه کپی/هاردلینک از فایل دانلودشده می‌سازه و لینک دانلود مستقیمش رو برمی‌گردونه.

    custom_filename: اسم انگلیسیِ دلخواهِ از قبل پاک‌سازی‌شده (با
    sanitize_ascii_filename) که کاربر انتخاب کرده. اگه داده بشه، به‌جای
    فال‌بک خودکار nightnote_<id> استفاده می‌شه (پسوند واقعی فایل حفظ می‌شه).

    برمی‌گردونه: (token, url) یا None اگه غیرفعال بود / خطا داد.
    """
    if not settings.DIRECT_LINK_ENABLED or not settings.DIRECT_LINK_PUBLIC_URL:
        logger.warning(
            f"DIRECT_LINK_ENABLED خاموشه یا DIRECT_LINK_PUBLIC_URL خالیه؛ "
            f"لینک مستقیم برای job {job_id} ساخته نشد."
        )
        return None

    try:
        token = secrets.token_urlsafe(16)
        dest_dir = Path(settings.DIRECT_LINK_DIR) / token
        dest_dir.mkdir(parents=True, exist_ok=True)

        if custom_filename:
            ext = file_path.suffix if file_path.suffix.isascii() else ""
            base = _safe_filename(custom_filename)
            filename = base if (ext and base.lower().endswith(ext.lower())) else f"{base}{ext}"
        else:
            filename = _ascii_filename(job_id, file_path.name)
        dest_path = dest_dir / filename

        try:
            # هاردلینک ارزون‌تره (بدون کپی واقعی دیتا) ولی فقط تو یه فایل‌سیستم/پارتیشن کار می‌کنه
            os.link(file_path, dest_path)
        except OSError:
            shutil.copy2(file_path, dest_path)

        url = build_url(token, filename)
        logger.info(f"لینک مستقیم برای job {job_id} ساخته شد: {url}")
        return token, url
    except Exception as e:
        logger.error(f"ساخت لینک مستقیم برای job {job_id} ناموفق بود: {e}")
        return None


def build_url(token: str, filename: str) -> str:
    base = settings.DIRECT_LINK_PUBLIC_URL.rstrip("/")
    return f"{base}/dl/{token}/{quote(filename)}"


def delete_direct_link(token: str):
    """پوشه‌ی مربوط به یه لینک مستقیم رو پاک می‌کنه (بعد از انقضا یا دستی)"""
    try:
        dest_dir = Path(settings.DIRECT_LINK_DIR) / token
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
            logger.info(f"لینک مستقیم {token} پاک شد (منقضی شد)")
    except Exception as e:
        logger.error(f"پاک‌سازی لینک مستقیم {token} ناموفق بود: {e}")
