"""
محدودکننده‌ی تعداد دانلود همزمان (MAX_CONCURRENT_DOWNLOADS) - واقعاً اجرا می‌شه.

چرا Redis؟ چون Celery معمولاً با چند پروسه/thread همزمان اجرا می‌شه (پرافورک)،
یه شمارنده‌ی ساده‌ی تو حافظه‌ی پایتون (مثل `count = 0` یا `threading.Semaphore`)
فقط تو یه پروسه معتبره و بین پروسه‌های دیگه‌ی worker مشترک نیست - یعنی عملاً
هیچ محدودیتی اعمال نمی‌کنه. Redis چون از قبل تو پروژه (broker/cache) استفاده
می‌شه، ساده‌ترین جای مشترک بین همه‌ی پروسه‌هاست.

چرا Hash به‌جای یه شمارنده‌ی INCR/DECR ساده؟
اگه از INCR/DECR ساده استفاده می‌شد و یه worker وسط دانلود کرش می‌کرد یا
kill -9 می‌شد (بدون این‌که DECR اجرا بشه)، اون اسلات برای همیشه اشغال
می‌موند و بعد از چند تا کرش، کل ظرفیت دانلود قفل می‌شد بدون این‌که هیچ
دانلود واقعی‌ای در حال اجرا باشه. با یه Hash از job_id -> زمان شروع،
قبل از هر تلاش برای گرفتن اسلات، ورودی‌های قدیمی‌تر از یه سقف منطقی (زمان
timeout دانلود + مهلت اضافه) به‌عنوان "باقی‌مونده‌ی یه worker کرش‌شده" در
نظر گرفته و پاک می‌شن - یعنی این مکانیزم خودش رو ترمیم می‌کنه، بدون نیاز
به فرآیند پاک‌سازی جدا.
"""
import time
from typing import Optional

import redis as redis_sync

from bot.config import settings

_HASH_KEY = "downloader_bot:active_downloads"

# باید به‌وضوح از سقف واقعی زمان یه دانلود بیشتر باشه، وگرنه یه دانلود کند
# ولی هنوز در حال انجام، اشتباهی "قدیمی/رهاشده" تشخیص داده می‌شه و یکی
# دیگه جای اسلاتش رو می‌گیره درحالی‌که هنوز داره واقعاً دانلود می‌شه.
_STALE_AFTER_SECONDS = settings.DOWNLOAD_TIMEOUT + 300

_client: Optional["redis_sync.Redis"] = None

# چک ظرفیت + رزرو اسلات باید یه عملیات atomic باشه، وگرنه دو تا worker که
# دقیقاً هم‌زمان try_acquire رو صدا می‌زنن، هر دو ممکنه hlen رو زیر سقف
# ببینن و هر دو hsetnx کنن - یعنی سقف واقعی رد می‌شه. با یه اسکریپت Lua،
# Redis کل این چک+نوشتن رو تو یه تراکنش تک‌رشته‌ای (بدون امکان interleave
# با دستور یه worker دیگه) اجرا می‌کنه.
_ACQUIRE_SCRIPT = """
local key = KEYS[1]
local job_id = ARGV[1]
local limit = tonumber(ARGV[2])
local now = ARGV[3]

if redis.call('HEXISTS', key, job_id) == 1 then
    return 1
end

if tonumber(redis.call('HLEN', key)) >= limit then
    return 0
end

redis.call('HSETNX', key, job_id, now)
return 1
"""

_acquire_script = None


def _get_client() -> "redis_sync.Redis":
    global _client, _acquire_script
    if _client is None:
        _client = redis_sync.Redis.from_url(settings.REDIS_URL, decode_responses=True)
        _acquire_script = _client.register_script(_ACQUIRE_SCRIPT)
    return _client


def _evict_stale(client: "redis_sync.Redis") -> None:
    now = time.time()
    entries = client.hgetall(_HASH_KEY)
    stale_ids = [job_id for job_id, started_at in entries.items() if now - float(started_at) > _STALE_AFTER_SECONDS]
    if stale_ids:
        client.hdel(_HASH_KEY, *stale_ids)


def try_acquire(job_id: int, limit: int) -> bool:
    """
    سعی می‌کنه یه اسلات دانلود برای job_id بگیره.
    True = اسلات گرفته شد، دانلود می‌تونه شروع بشه.
    False = ظرفیت پره، caller باید صبر کنه یا بعداً دوباره امتحان کنه.
    """
    if limit <= 0:
        return True  # ۰ یا منفی یعنی محدودیت غیرفعاله

    client = _get_client()
    _evict_stale(client)

    # اجرای اتمیک: چک وجود قبلی + چک ظرفیت + رزرو، همه تو یه تراکنش Redis
    # (بدون این‌که یه worker دیگه بین این مراحل فرصت interleave پیدا کنه).
    acquired = _acquire_script(keys=[_HASH_KEY], args=[str(job_id), limit, time.time()])
    return bool(acquired)


def release(job_id: int) -> None:
    """آزاد کردن اسلات بعد از تموم شدن دانلود (چه موفق، چه ناموفق، چه کنسل‌شده)."""
    try:
        _get_client().hdel(_HASH_KEY, str(job_id))
    except Exception:
        # آزاد نشدن اسلات یه چیز بحرانی نیست (مکانیزم stale-eviction بالا
        # خودش رو ترمیم می‌کنه)، پس اینجا نباید کل تسک رو fail کنه.
        pass


def active_count() -> int:
    """تعداد فعلی دانلودهای در حال اجرا (برای مانیتورینگ/دیباگ)."""
    client = _get_client()
    _evict_stale(client)
    return client.hlen(_HASH_KEY)
