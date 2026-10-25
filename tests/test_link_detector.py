"""
تست‌های واحد برای utils/link_detector.py

این ماژول رو انتخاب کردیم چون منطق خالصه (بدون I/O، بدون شبکه، بدون دیتابیس)
و مستقیماً تصمیم می‌گیره که هر لینک قراره از کدوم مسیر دانلود بشه - یعنی هر
باگ توش (مثلاً تشخیص اشتباه یه ریلز اینستاگرام به‌جای پست عادی) مستقیم روی
تجربه‌ی کاربر اثر می‌ذاره، ولی چون کاملاً pure function هست، تست کردنش هزینه‌ی
تقریباً صفر داره.

اجرا (از ریشه‌ی پروژه، بعد از `pip install pytest`):
    pytest tests/test_link_detector.py -v
"""
import pytest

from utils.link_detector import LinkDetector, SiteType, ContentType, detect_link


# ---------------------------------------------------------------------------
# تشخیص سایت
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", SiteType.YOUTUBE),
        ("https://youtu.be/dQw4w9WgXcQ", SiteType.YOUTUBE_SHORTS),  # youtu.be فعلاً همیشه SHORTS تشخیص داده می‌شه
        ("https://www.youtube.com/shorts/abcDEF123", SiteType.YOUTUBE_SHORTS),
        ("https://music.youtube.com/watch?v=abcDEF123", SiteType.YOUTUBE_MUSIC),
        ("https://open.spotify.com/track/abc123", SiteType.SPOTIFY),
        ("https://soundcloud.com/artist/track", SiteType.SOUNDCLOUD),
        ("https://www.instagram.com/p/AbC123xyz/", SiteType.INSTAGRAM),
        ("https://www.instagram.com/reel/AbC123xyz/", SiteType.INSTAGRAM_REEL),
        ("https://www.tiktok.com/@user/video/1234567890", SiteType.TIKTOK),
        ("https://twitter.com/user/status/12345", SiteType.TWITTER),
        ("https://x.com/user/status/12345", SiteType.TWITTER),
        ("https://www.aparat.com/v/abcDE12", SiteType.APARAT),
        ("https://vimeo.com/12345678", SiteType.VIMEO),
        ("https://www.pinterest.com/pin/12345/", SiteType.PINTEREST),
        ("https://apkpure.com/some-app/com.example.app", SiteType.APKPURE),
        ("https://this-domain-does-not-exist-anywhere.zz/page", SiteType.UNKNOWN),
    ],
)
def test_detect_site(url, expected):
    assert LinkDetector.detect_site(url) == expected


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://cdn.example.com/files/video_final.mp4", SiteType.DIRECT_VIDEO),
        ("https://cdn.example.com/files/song.mp3", SiteType.DIRECT_AUDIO),
        ("https://cdn.example.com/files/app.apk", SiteType.DIRECT_FILE),
        ("https://cdn.example.com/files/archive.zip", SiteType.DIRECT_FILE),
        # کوئری‌استرینگ نباید تشخیص پسوند رو خراب کنه (این همون باگیه که تو
        # ربات واقعی دیدیم: لینک CDN با توکن امضاشده تو کوئری‌استرینگ)
        (
            "https://cdn.example.com/dl/01/file_a1b2.mp4"
            "?t=abc&s=123&e=456&node=xyz&n=Some.File.Name.mp4&d=1",
            SiteType.DIRECT_VIDEO,
        ),
    ],
)
def test_detect_site_direct_files_ignore_query_string(url, expected):
    assert LinkDetector.detect_site(url) == expected


def test_detect_site_case_insensitive():
    assert LinkDetector.detect_site("HTTPS://WWW.YOUTUBE.COM/WATCH?V=ABC") == SiteType.YOUTUBE


# ---------------------------------------------------------------------------
# استخراج شناسه (id) از لینک
# ---------------------------------------------------------------------------

def test_extract_id_youtube():
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert LinkDetector.extract_id(url, SiteType.YOUTUBE) == "dqw4w9wgxcq"  # extract_id لوکیس می‌کنه


def test_extract_id_instagram():
    url = "https://www.instagram.com/p/AbC123xyz/"
    assert LinkDetector.extract_id(url, SiteType.INSTAGRAM) == "abc123xyz"


def test_extract_id_unknown_site_returns_none():
    assert LinkDetector.extract_id("https://example.com/x", SiteType.UNKNOWN) is None


# ---------------------------------------------------------------------------
# تشخیص پلی‌لیست
# ---------------------------------------------------------------------------

def test_is_playlist_youtube():
    assert LinkDetector.is_playlist(
        "https://www.youtube.com/playlist?list=PL123", SiteType.YOUTUBE
    ) is True


def test_is_playlist_single_video_is_false():
    assert LinkDetector.is_playlist(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ", SiteType.YOUTUBE
    ) is False


def test_is_playlist_soundcloud_sets():
    assert LinkDetector.is_playlist(
        "https://soundcloud.com/artist/sets/my-album", SiteType.SOUNDCLOUD
    ) is True


# ---------------------------------------------------------------------------
# نوع محتوا
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "site_type, is_playlist, expected",
    [
        (SiteType.YOUTUBE, False, ContentType.VIDEO),
        (SiteType.SPOTIFY, False, ContentType.AUDIO),
        (SiteType.DIRECT_AUDIO, False, ContentType.AUDIO),
        (SiteType.DIRECT_VIDEO, False, ContentType.VIDEO),
        (SiteType.DIRECT_FILE, False, ContentType.FILE),
        (SiteType.GITHUB, False, ContentType.FILE),
        (SiteType.YOUTUBE, True, ContentType.PLAYLIST),  # is_playlist همیشه اولویت داره
    ],
)
def test_get_content_type(site_type, is_playlist, expected):
    assert LinkDetector.get_content_type(site_type, is_playlist) == expected


# ---------------------------------------------------------------------------
# استخراج لینک از وسط متن آزاد (مثلاً وقتی کاربر قبل/بعد لینک توضیح می‌نویسه)
# ---------------------------------------------------------------------------

def test_extract_url_from_surrounding_text():
    text = "این ویدیو رو ببین:\nhttps://www.youtube.com/watch?v=dQw4w9WgXcQ\nخیلی باحاله"
    assert LinkDetector.extract_url(text) == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_extract_url_no_link_returns_none():
    assert LinkDetector.extract_url("سلام، امروز حالت چطوره؟") is None


def test_extract_url_strips_trailing_punctuation():
    text = "لینکش اینه: https://example.com/page."
    assert LinkDetector.extract_url(text) == "https://example.com/page"


def test_is_valid_url_true_for_http_link():
    assert LinkDetector.is_valid_url("https://example.com/watch?v=1") is True


def test_is_valid_url_false_for_plain_text():
    assert LinkDetector.is_valid_url("این یه لینک نیست") is False


# ---------------------------------------------------------------------------
# تابع سطح‌بالای detect_link (site + content type با هم)
# ---------------------------------------------------------------------------

def test_detect_link_combines_site_and_content_type():
    site, content = detect_link("https://open.spotify.com/track/abc123")
    assert site == SiteType.SPOTIFY
    assert content == ContentType.AUDIO


def test_detect_link_playlist():
    site, content = detect_link("https://www.youtube.com/playlist?list=PL123")
    assert site == SiteType.YOUTUBE
    assert content == ContentType.PLAYLIST
