"""
پاک‌سازی اسم فایل - سه سناریوی متمایز که قبلاً پراکنده تو سه فایل بودن:

۱. sanitize_ascii_filename  - اسمِ دلخواهِ کاربر برای لینک مستقیم
   (فقط ASCII مجاز؛ فارسی/عربی/کاراکتر خاص → None)
۲. sanitize_remote_filename - اسمی که سرورِ مقصد از Content-Disposition/URL
   می‌فرسته (باید path traversal مثل ../../ و NUL رو خنثی کنه - وگرنه
   ورکرِ root دار فایل خارج از پوشه‌ی temp می‌نویسه)
۳. safe_upload_filename     - اسم فایلی که کاربر خودش به بات آپلود کرده
   (هر چیز غیرمجاز → underscore، سقف ۱۵۰ کاراکتر)

خالص و بدون وابستگی - مستقیم تست‌پذیر (tests/test_filename.py).
"""
import re
from typing import Optional

# فقط حروف/عدد انگلیسی، نقطه، خط‌تیره، زیرخط و فاصله مجازن
_ASCII_CLEAN_RE = re.compile(r'[^A-Za-z0-9._\- ]')
# برای اسم آپلودی کاربر: غیرمجازها حذف نمی‌شن، به underscore تبدیل می‌شن
_UPLOAD_UNSAFE_RE = re.compile(r'[^A-Za-z0-9._\- ]')


def sanitize_ascii_filename(raw: str) -> Optional[str]:
    """
    اسم دلخواهی که کاربر برای لینک مستقیم فرستاده رو پاک‌سازی می‌کنه: فقط
    حروف/عدد انگلیسی، فاصله، نقطه، خط‌تیره و زیرخط مجازن (بدون فارسی/عربی و
    بدون کاراکتر خاص که تو URL یا مسیر فایل مشکل‌ساز بشه).

    اگه بعد از پاک‌سازی چیزی قابل‌استفاده نمونه (مثلاً کاربر فقط فارسی
    فرستاده بود)، None برمی‌گردونه.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    cleaned = _ASCII_CLEAN_RE.sub("", raw).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned)
    cleaned = cleaned.strip("._-")
    if not cleaned:
        return None
    return cleaned[:100]


def sanitize_remote_filename(name: str) -> str:
    """
    اسم فایلِ رسیده از Content-Disposition/URL رو به یه اسم امنِ محلی تبدیل
    می‌کنه. بدون این، یه سرور مخرب می‌تونه filename="../../etc/cron.d/evil"
    بفرسته و فایل خارج از پوشه‌ی temp نوشته بشه (path traversal).
    تهی برمی‌گرده اگه چیزی باقی نمونه (caller فال‌بک می‌ذاره).
    """
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = name.split("?")[0].split("#")[0].replace("\x00", "").strip()
    name = name.lstrip(".")  # ".." و "." و فایل‌های مخفی رو هم خنثی می‌کنه
    return name[:200]


def safe_upload_filename(raw: str) -> str:
    """
    اسم فایل آپلودیِ کاربر: هر کاراکتر غیرمجاز → underscore، سقف ۱۵۰
    کاراکتر. تهی برمی‌گرده اگه چیزی نموند (caller اسم پیش‌فرض می‌ذاره).
    """
    return _UPLOAD_UNSAFE_RE.sub("_", (raw or "").strip())[:150]
