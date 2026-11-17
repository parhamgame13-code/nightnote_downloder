"""
ذخیره‌سازی فایل‌های نسخه‌ی وب.

مدل ذخیره‌سازی دقیقاً آینه‌ی utils/direct_links.py هست: هر فایل زیر یه
پوشه‌ی توکن‌دارِ تصادفی داخل DIRECT_LINK_DIR می‌ره
(DIRECT_LINK_DIR/<token>/<filename>) و از مسیر /dl/<token>/<filename> سرو
می‌شه. تفاوت‌های وب با مسیر بات:

۱. وب به DIRECT_LINK_ENABLED وابسته نیست. فایل‌های وب همیشه باید از خودِ
   سایت سرو بشن؛ وگرنه سایت بی‌معنی می‌شه. فیلدهای direct_link_* جاب
   همچنان با DownloadJobCRUD.set_direct_link ثبت می‌شن تا آمار/تمدید/QR
   بات هم همین فایل‌ها رو ببینه.
۲. کش وب جدا از جدول file_cache هست. رکوردهای FileCache فقط telegram_file_id
   دارن (فایل روی سرورهای تلگرام) و برای سرو فایل از وب قابل استفاده
   نیستن. کش وب روی دیسک و زیر DIRECT_LINK_DIR/.webcache/<hash>/ نگه‌داری
   می‌شه و با همون get_cache_hash(url, quality, file_type) کلید می‌خوره.
   پوشه‌ی .webcache مستقیماً از /dl/ سرو نمی‌شه؛ موقع cache hit، یه کپی/
   هاردلینک تازه زیر یه توکن جدید ساخته می‌شه.
"""
import json
import os
import secrets
import shutil
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from loguru import logger

from bot.config import settings
from utils.filename import sanitize_ascii_filename, sanitize_remote_filename

_CACHE_DIRNAME = ".webcache"


def base_dir() -> Path:
    """پوشه‌ی ریشه‌ی فایل‌های سروشدنی (ساخته می‌شه اگه نباشه)."""
    base = Path(settings.DIRECT_LINK_DIR).resolve()
    base.mkdir(parents=True, exist_ok=True)
    return base


def cache_dir() -> Path:
    """پوشه‌ی کش فایل‌های وب (داخل DIRECT_LINK_DIR ولی غیرقابل‌سرو مستقیم)."""
    cache = base_dir() / _CACHE_DIRNAME
    cache.mkdir(parents=True, exist_ok=True)
    return cache


def _is_safe_token(token: str) -> bool:
    """توکن باید یه اسمِ تکیِ پوشه باشه، نه مسیر و نه پوشه‌ی مخفی/سیستمی."""
    if not token or token.startswith("."):
        return False
    if "/" in token or "\\" in token or ".." in token:
        return False
    return True


def resolve_served_file(token: str, filename: str) -> Path:
    """
    مسیر فایلِ سروشدنی رو از روی توکن/اسم‌فایلِ URL برمی‌گردونه.

    اگه توکن/اسم‌فایل ناامن باشه (traversal) ValueError می‌ده و اگه فایل
    نباشه FileNotFoundError. منطق traversal دقیقاً مثل
    webserver/direct_link_server.py هست.
    """
    if not _is_safe_token(token):
        raise ValueError("درخواست نامعتبر")
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        raise ValueError("درخواست نامعتبر")

    base = base_dir()
    file_path = (base / token / filename).resolve()
    try:
        file_path.relative_to(base)
    except ValueError:
        raise ValueError("دسترسی غیرمجاز") from None
    if not file_path.is_file():
        raise FileNotFoundError("این لینک منقضی شده یا وجود نداره.")
    return file_path


def _stored_filename(job_id: int, original_name: str, custom_filename: Optional[str] = None) -> str:
    """اسم فایلِ ذخیره‌شده: اسم دلخواه کاربر (ASCII، مثل بات) یا اسم اصلیِ
    پاک‌سازی‌شده؛ اگه هیچ‌کدوم نشد، فال‌بک nightnote_<id>.<ext>."""
    ext = Path(original_name).suffix
    if ext and not ext.isascii():
        ext = ""
    if custom_filename:
        cleaned = sanitize_ascii_filename(custom_filename)
        if cleaned:
            base = cleaned
            if ext and not base.lower().endswith(ext.lower()):
                base = f"{base}{ext}"
            return base
    safe = sanitize_remote_filename(original_name)
    if safe and safe not in (".", ".."):
        return safe[:200]
    return f"nightnote_{job_id}{ext}"


def store_file(
    job_id: int, src_path: Path, custom_filename: Optional[str] = None
) -> tuple[str, str, Path]:
    """
    یه هاردلینک (یا کپی، اگه هاردلینک ممکن نبود) از فایل دانلودشده زیر یه
    پوشه‌ی توکن‌دار می‌سازه.

    برمی‌گردونه: (token, filename, dest_path). مسیر URL نسبی با
    download_url_path ساخته می‌شه.
    """
    token = secrets.token_urlsafe(16)
    dest_dir = base_dir() / token
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = _stored_filename(job_id, src_path.name, custom_filename)
    dest_path = dest_dir / filename
    try:
        os.link(src_path, dest_path)
    except OSError:
        shutil.copy2(src_path, dest_path)
    logger.info(f"فایل وب برای job {job_id} ذخیره شد: {dest_path}")
    return token, filename, dest_path


def download_url_path(token: str, filename: str) -> str:
    """مسیر نسبی دانلود (/dl/<token>/<filename>) با quote کردن اسم فایل."""
    return f"/dl/{token}/{quote(filename)}"


def build_download_url(token: str, filename: str, request_base: str = "") -> str:
    """
    URL کامل دانلود: اگه WEB_PUBLIC_URL ست باشه از اون استفاده می‌شه، وگرنه
    از آدرس پایه‌ی همون درخواست (request_base) و در نهایت مسیر نسبی.
    """
    path = download_url_path(token, filename)
    base = (settings.WEB_PUBLIC_URL or "").rstrip("/")
    if base:
        return f"{base}{path}"
    if request_base:
        return f"{request_base.rstrip('/')}{path}"
    return path


# ---------------------------------------------------------------------------
# کش فایل‌های وب (روی دیسک، جدا از FileCache تلگرام)
# ---------------------------------------------------------------------------

def get_cached(cache_hash: str) -> Optional[tuple[Path, dict]]:
    """اگه برای این hash یه فایل کش‌شده‌ی سالم هست، (مسیر، متادیتا) رو بده."""
    entry_dir = cache_dir() / cache_hash
    meta_path = entry_dir / "meta.json"
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        file_path = entry_dir / meta["filename"]
    except Exception:
        return None
    if not file_path.is_file() or file_path.stat().st_size == 0:
        return None
    return file_path, meta


def put_cache(cache_hash: str, src_path: Path, meta: dict) -> None:
    """فایل رو تو کش وب ذخیره می‌کنه (هاردلینک/کپی + meta.json)."""
    entry_dir = cache_dir() / cache_hash
    entry_dir.mkdir(parents=True, exist_ok=True)
    filename = sanitize_remote_filename(src_path.name) or "file"
    dest = entry_dir / filename
    if not dest.exists():
        try:
            os.link(src_path, dest)
        except OSError:
            shutil.copy2(src_path, dest)
    meta = {**meta, "filename": filename, "size_bytes": dest.stat().st_size}
    (entry_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )


def store_from_cache(job_id: int, cache_hash: str, custom_filename: Optional[str] = None):
    """
    از روی یه ورودی کش، یه فایل سروشدنیِ تازه زیر توکن جدید می‌سازه.
    برمی‌گردونه: (token, filename, dest_path, meta) یا None اگه کش نباشه.
    """
    cached = get_cached(cache_hash)
    if not cached:
        return None
    file_path, meta = cached
    token, filename, dest_path = store_file(job_id, file_path, custom_filename)
    return token, filename, dest_path, meta


def delete_token_dir(token: str) -> None:
    """پوشه‌ی یه توکن رو پاک می‌کنه (بعد از انقضا)."""
    if not _is_safe_token(token):
        return
    try:
        dest_dir = base_dir() / token
        if dest_dir.is_dir():
            shutil.rmtree(dest_dir)
            logger.info(f"پوشه‌ی لینک وب {token} پاک شد (منقضی شد)")
    except Exception as e:
        logger.warning(f"پاک‌سازی پوشه‌ی لینک وب {token} ناموفق بود: {e}")


def cleanup_expired(now: Optional[float] = None) -> int:
    """
    پوشه‌های توکن‌داری که از DIRECT_LINK_EXPIRE_SECONDS قدیمی‌ترن رو پاک
    می‌کنه (بر اساس mtime پوشه). پوشه‌ی کش (.webcache) دست نمی‌خوره.
    برمی‌گردونه: تعداد پوشه‌های پاک‌شده.
    """
    now = now if now is not None else time.time()
    expire = settings.DIRECT_LINK_EXPIRE_SECONDS
    if expire <= 0:
        return 0
    base = base_dir()
    removed = 0
    for entry in base.iterdir():
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        try:
            if now - entry.stat().st_mtime > expire:
                shutil.rmtree(entry)
                removed += 1
        except Exception as e:
            logger.warning(f"پاک‌سازی پوشه‌ی منقضی {entry.name} ناموفق بود: {e}")
    if removed:
        logger.info(f"پاک‌سازی وب: {removed} پوشه‌ی منقضی حذف شد")
    return removed
