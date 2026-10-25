"""
تست‌های واحد برای utils/cache_key.py

اجرا (از ریشه‌ی پروژه، بعد از `pip install pytest`):
    pytest tests/test_cache_key.py -v
"""
from utils.cache_key import get_cache_hash, normalize_url


# ---------------------------------------------------------------------------
# query در کلید کش
# ---------------------------------------------------------------------------

def test_different_query_ids_produce_different_hashes():
    # باگ قبلی: query کلاً از hash حذف می‌شد و دو فایل متفاوت که id‌شون تو
    # query ـه، یه کلید کش مشترک می‌گرفتن (فایل غلط از کش سرو می‌شد)
    h1 = get_cache_hash("https://cdn.example.com/dl/file.mp4?id=111", "best", "document")
    h2 = get_cache_hash("https://cdn.example.com/dl/file.mp4?id=222", "best", "document")
    assert h1 != h2


def test_query_param_order_is_normalized():
    h1 = normalize_url("https://cdn.example.com/file.mp4?a=1&b=2")
    h2 = normalize_url("https://cdn.example.com/file.mp4?b=2&a=1")
    assert h1 == h2


def test_tracking_query_ignored_for_platforms():
    # پلتفرم‌ها هویت‌شون از path ـه؛ پارامترهای ردیابی (مثل si= اسپاتیفای)
    # نباید کلید کش رو بشکنن
    h1 = get_cache_hash("https://open.spotify.com/track/abc", "best", "audio")
    h2 = get_cache_hash("https://open.spotify.com/track/abc?si=xyz123", "best", "audio")
    assert h1 == h2


def test_youtube_strips_extra_query_but_keeps_video_id():
    h1 = get_cache_hash("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "best", "video")
    h2 = get_cache_hash("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=90s", "best", "video")
    assert h1 == h2

    h3 = get_cache_hash("https://www.youtube.com/watch?v=otherID", "best", "video")
    assert h1 != h3


# ---------------------------------------------------------------------------
# نرمال‌سازی عمومی (رفتارهای قبلی که نباید بشکنن)
# ---------------------------------------------------------------------------

def test_scheme_and_netloc_lowercased_path_preserved():
    # آی‌دی محتوا case-sensitive ـه؛ نباید lowercase بشه
    n = normalize_url("HTTPS://WWW.Instagram.COM/p/AbCdEf/")
    assert n == "https://www.instagram.com/p/AbCdEf"


def test_youtube_short_url_normalized():
    n = normalize_url("https://youtu.be/dQw4w9WgXcQ")
    assert n == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
