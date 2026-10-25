"""
ایجاد و مدیریت cache key‌ها
"""
import hashlib
from urllib.parse import urlparse, parse_qs
from typing import Optional


# دامنه‌های پلتفرمی که هویت محتوا از مسیر (path) میاد و query فقط پارامتر
# ردیابیه (si=، utm_*، t= و ...) - برای این‌ها query از کلید کش حذف می‌شه تا
# لینک‌های share‌شده‌ی متفاوتِ همون پست به یه کش برسن. برای بقیه (هاست‌های
# فایل/CDN که id فایل اغلب تو query ـه) query بخشی از کلید می‌مونه؛ بدون این
# دو فایل متفاوت با مسیر یکسان (`/dl/file.mp4?id=1` مقابل `id=2`) یه hash
# مشترک می‌گرفتن و فایلِ غلط از کش سرو می‌شد.
_QUERY_IS_NOISE_HOSTS = (
    "youtube.com", "youtu.be", "spotify.com", "soundcloud.com",
    "apple.com", "instagram.com", "tiktok.com", "twitter.com", "x.com",
    "pinterest.com", "pin.it", "aparat.com", "vimeo.com", "facebook.com",
    "github.com", "githubusercontent.com", "codeload.github.com",
)


def normalize_url(url: str) -> str:
    """نرمال‌کردن URL برای cache"""
    # تشخیص اگه لینک
    # (چک case-insensitive - قبلاً startswith حساس به حروف بزرگ/کوچیک بود و
    # لینک «HTTPS://...» دوباره پیشوند https:// می‌گرفت و کل نرمال‌سازی خراب می‌شد)
    if not url.lower().startswith(('http://', 'https://')):
        url = 'https://' + url

    # نکته‌ی مهم: فقط scheme/domain رو lowercase می‌کنیم، نه کل URL رو.
    # چون آی‌دی ویدیوی یوتیوب، شورت‌کدِ پست اینستاگرام، و آی‌دی پین پینترست
    # case-sensitive هستن (base64-like؛ حروف بزرگ/کوچیک معنی متفاوتی دارن).
    # قبلاً با url.lower() رو کل URL، دو تا ویدیوی کاملاً متفاوت که فقط تو
    # حروف بزرگ/کوچیکِ آی‌دی‌شون فرق داشتن، یه cache hash یکسان می‌گرفتن -
    # یعنی ممکن بود فایل ویدیوی اشتباه از کش برگرده!
    parsed = urlparse(url)
    parsed = parsed._replace(scheme=parsed.scheme.lower(), netloc=parsed.netloc.lower())

    # فقط scheme + netloc + path — query params نمی‌خوریم (برای اینستاگرام و دیگر)
    normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    # یوتیوب: همیشه لینک نرمال شده (فقط v=؛ تایم‌استمپ t= و بقیه دور ریخته می‌شن)
    if 'youtube' in normalized or 'youtu.be' in normalized:
        # استخراج video ID
        params = parse_qs(parsed.query)
        video_id = params.get('v', [None])[0]
        if not video_id and 'youtu.be' in normalized:
            video_id = parsed.path.strip('/')
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"

    # بقیه‌ی دامنه‌ها: query رو (نرمال‌شده) نگه می‌داریم مگر برای پلتفرم‌های بالا
    if parsed.query and not any(
        h in parsed.netloc.lower() for h in _QUERY_IS_NOISE_HOSTS
    ):
        # ترتیب پارامترها نرمال می‌شه که ?a=1&b=2 و ?b=2&a=1 یکی حساب بشن
        normalized += "?" + "&".join(sorted(parsed.query.split("&")))

    return normalized.rstrip('/')


def get_cache_hash(url: str, quality: str = "best", file_type: Optional[str] = None) -> str:
    """ایجاد hash برای cache

    نکته‌ی مهم: file_type رو هم تو کلید کش وارد می‌کنیم. بدون این، یه پست
    عکسی (که همیشه با quality="best" کش می‌شه چون منوی کیفیت نداره) دقیقاً
    همون cache key رو با یه ویدیوی دیگه که کاربر روش "بهترین کیفیت" زده
    سهیم می‌شد - یعنی اگه یه لینک یه‌بار (مثلاً به‌خاطر یه باگ) اشتباهی
    عکس/ویدیو تشخیص داده می‌شد و کش می‌شد، همون نتیجه‌ی غلط برای همیشه زیر
    همون لینک گیر می‌کرد، حتی بعد از فیکس شدن باگ - چون هش کش هیچ‌وقت عوض
    نمی‌شد. اضافه‌کردن file_type هم از این تصادم جلوگیری می‌کنه، هم باعث
    می‌شه رکوردهای کشِ قدیمیِ احتمالاً آلوده (از قبل از این تغییر) دیگه
    match نشن.
    """
    norm_url = normalize_url(url)
    cache_key = f"{norm_url}|{quality}|{file_type or ''}"
    
    # SHA256 hash
    return hashlib.sha256(cache_key.encode()).hexdigest()
