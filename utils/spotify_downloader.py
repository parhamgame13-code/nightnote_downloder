"""
دانلودر اسپاتیفای - نسخه‌ی مبتنی بر تگ‌های meta خودِ صفحه.

سه رویکرد قبلی امتحان شد:
  ۱) spotifyscraper (پکیج pip) - نگهداری‌نشده، شکننده.
  ۲) spotdl (که BlueKnight هم رو دسکتاپ استفاده می‌کنه) - نیاز به Client
     ID/Secret اختصاصی داره که از فوریه‌ی ۲۰۲۶ ساختش تو Developer Dashboard
     اسپاتیفای منوط به داشتن اکانت Premium شده؛ بدون premium، کردنشیال
     پیش‌فرض/مشترکِ spotdl هم این‌روزها به‌طور گسترده rate-limit شده و
     باعث هنگ‌کردن بی‌صدا روی «Processing query» می‌شه (باگ شناخته‌شده و
     تکرارشونده تو ایشوهای گیت‌هاب خودِ spotDL).
  ۳) همینی که الان پیاده‌سازی شده: خودِ صفحه‌ی عمومی هر ترک اسپاتیفای
     (بدون نیاز به لاگین) یه سری تگ meta رسمی و ساختاریافته داره که
     دقیقاً برای همین منظور (preview لینک تو تلگرام/توییتر/فیسبوک) گذاشته
     شده - یعنی هیچ‌وقت قرار نیست بلاک بشه، چون کل هدفش خونده‌شدن توسط
     بات‌هاست:
       <meta property="og:title" content="اسم آهنگ">
       <meta property="og:image" content="لینک کاور">
       <meta name="music:musician_description" content="اسم خواننده">
       <meta property="og:description" content="خواننده · آلبوم · Song · سال">
     این‌ها ثابت و مستنداتی‌ان (Open Graph یه استاندارد رسمیه)، پس خیلی
     پایدارترن نسبت به هر سه راه قبلی.

برای گرفتن HTML از curl_cffi استفاده می‌کنیم (که TLS fingerprint مرورگر
واقعی رو شبیه‌سازی می‌کنه، برخلاف urllib/requests معمولی که بعضی وقتا با
چالش‌های ضد-بات مواجه می‌شن). دانلود واقعیِ صدا هم مثل قبل با yt-dlp و
جست‌وجوی نام آهنگ تو یوتیوب (ytsearch1) تو worker/tasks.py انجام می‌شه.
"""
import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from loguru import logger

_TRACK_RE = re.compile(r"open\.spotify\.com/(?:intl-\w+/)?track/([A-Za-z0-9]+)")
_ALBUM_OR_PLAYLIST_RE = re.compile(
    r"open\.spotify\.com/(?:intl-\w+/)?(?:album|playlist)/([A-Za-z0-9]+)"
)
# برای پیدا کردن لینک تک‌تکِ ترک‌ها داخل HTML صفحه‌ی پلی‌لیست/آلبوم
_TRACK_IN_PAGE_RE = re.compile(r"open\.spotify\.com/track/([A-Za-z0-9]{15,25})")

MAX_PLAYLIST_TRACKS = 40
_FETCH_TIMEOUT = 20


def _meta_content(html: str, *, prop: Optional[str] = None, name: Optional[str] = None) -> Optional[str]:
    """مقدار content یه تگ <meta property="..."> یا <meta name="..."> رو
    مستقل از ترتیب اتریبیوت‌ها استخراج می‌کنه."""
    key, value = ("property", prop) if prop else ("name", name)
    pattern = re.compile(
        rf'<meta[^>]+{key}=["\']{re.escape(value)}["\'][^>]+content=["\']([^"\']*)["\']'
        rf'|<meta[^>]+content=["\']([^"\']*)["\'][^>]+{key}=["\']{re.escape(value)}["\']',
        re.IGNORECASE,
    )
    m = pattern.search(html)
    if not m:
        return None
    raw = m.group(1) if m.group(1) is not None else m.group(2)
    return _unescape_html(raw) if raw else None


def _unescape_html(s: str) -> str:
    import html as _html
    return _html.unescape(s).strip()


@dataclass
class SpotifyTrack:
    title: str
    artist: Optional[str]
    album: Optional[str]
    cover_url: Optional[str]
    spotify_url: str


def is_track_url(url: str) -> bool:
    return bool(_TRACK_RE.search(url))


def is_playlist_or_album(url: str) -> bool:
    return bool(_ALBUM_OR_PLAYLIST_RE.search(url))


def _fetch_html_sync(url: str) -> Optional[str]:
    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        logger.error("curl_cffi نصب نیست - pip install curl_cffi لازمه")
        return None

    try:
        resp = curl_requests.get(url, impersonate="chrome", timeout=_FETCH_TIMEOUT)
    except Exception as e:
        logger.error(f"گرفتن صفحه‌ی اسپاتیفای {url} ناموفق بود: {e}")
        return None

    if resp.status_code != 200:
        logger.error(f"صفحه‌ی اسپاتیفای {url} کد {resp.status_code} برگردوند")
        return None
    return resp.text


async def _fetch_html(url: str) -> Optional[str]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_html_sync, url)


def _parse_track_meta(html: str, url: str) -> Optional[SpotifyTrack]:
    title = _meta_content(html, prop="og:title")
    if not title:
        return None

    artist = _meta_content(html, name="music:musician_description")
    cover_url = _meta_content(html, prop="og:image")
    album = None

    # og:description معمولاً به فرمت «خواننده · آلبوم · Song · سال»ه؛ اگه
    # music:musician_description نبود این هم به‌عنوان فال‌بک استفاده می‌شه
    description = _meta_content(html, prop="og:description")
    if description:
        parts = [p.strip() for p in description.split("·")]
        if not artist and parts:
            artist = parts[0] or None
        if len(parts) >= 2 and parts[1] and parts[1].lower() not in ("song", "single", "ep", "album"):
            album = parts[1]

    return SpotifyTrack(
        title=title, artist=artist, album=album, cover_url=cover_url, spotify_url=url,
    )


async def fetch_spotify_track(url: str) -> Optional[SpotifyTrack]:
    if not is_track_url(url):
        return None
    html = await _fetch_html(url)
    if not html:
        return None
    track = _parse_track_meta(html, url)
    if not track:
        logger.error(f"پارس تگ‌های meta صفحه‌ی اسپاتیفای {url} ناموفق بود")
    return track


async def fetch_spotify_playlist_tracks(url: str) -> list[str]:
    """لیست URLهای تک‌تکِ ترک‌های یه پلی‌لیست/آلبوم اسپاتیفای رو از خودِ
    HTML صفحه (بدون API) استخراج می‌کنه. هر URL بعداً از همون مسیر تک‌آهنگِ
    دانلود رد می‌شه."""
    html = await _fetch_html(url)
    if not html:
        return []

    seen: dict[str, None] = {}
    for track_id in _TRACK_IN_PAGE_RE.findall(html):
        track_url = f"https://open.spotify.com/track/{track_id}"
        seen.setdefault(track_url, None)

    urls = list(seen.keys())
    if not urls:
        logger.error(
            f"هیچ ترکی از HTML صفحه‌ی پلی‌لیست/آلبوم {url} استخراج نشد - ممکنه "
            f"لیست ترک‌ها لِیزی‌لود بشه و تو HTML اولیه نباشه"
        )
    return urls[:MAX_PLAYLIST_TRACKS]


def build_search_query(track: SpotifyTrack) -> str:
    if track.artist:
        return f"{track.artist} {track.title}"
    return track.title


async def tag_audio_file(path: Path, track: SpotifyTrack) -> None:
    """چون دانلود صدا از یوتیوب انجام می‌شه (نه از خودِ اسپاتیفای)، تگ ID3
    فایل نهایی رو با متادیتای واقعیِ اسپاتیفای (نه عنوان خامِ ویدیوی
    یوتیوب) جایگزین می‌کنیم."""
    from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TALB

    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()

    if track.title:
        tags.setall("TIT2", [TIT2(encoding=3, text=track.title)])
    if track.artist:
        tags.setall("TPE1", [TPE1(encoding=3, text=track.artist)])
    if track.album:
        tags.setall("TALB", [TALB(encoding=3, text=track.album)])

    tags.save(path, v2_version=3)
