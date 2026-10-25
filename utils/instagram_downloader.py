"""
دانلود مستقیم عکس/کاروسل و موزیک پس‌زمینه از اینستاگرام، بدون yt-dlp.

چرا این ماژول لازم بود؟ yt-dlp روی اینستاگرام فقط ویدیو/ریلز رو پشتیبانی
می‌کنه. برای پستی که فقط عکس (یا عکس/کاروسل + موزیک پس‌زمینه) باشه، اکسترکتورش
اصلاً فرمتی برنمی‌گردونه و با خطای "No video formats found!" fail می‌کنه -
این یه محدودیت شناخته‌شده‌ی خودِ yt-dlp روی اینستاگرامه (نگاه کن به ایشوهای
GitHub خودشون)، نه چیزی که با تنظیمات ما درست بشه.

این ماژول به‌جاش مستقیم از همون API داخلیِ اپ موبایل اینستاگرام استفاده
می‌کنه (با هدر X-IG-App-ID که تو باندل جاوااسکریپت وب‌کلاینت instagram.com هم
عمومی و قابل مشاهده‌ست) - برای پست‌های عمومی نیازی به لاگین نیست. اگه این
مسیر جواب نداد (مثلاً پست محدودشده)، یه fallback ساده‌تر از روی og:image/
og:video خودِ صفحه هم داریم (فقط تک‌آیتم، بدون کاروسل/موزیک).
"""
import re
import html as html_module
import http.cookiejar
from dataclasses import dataclass, field
from typing import Optional

import httpx
from loguru import logger

# App ID عمومیِ وب‌کلاینت instagram.com - مخصوص یه اکانت یا secret نیست
APP_ID = "936619743392459"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


@dataclass
class InstagramMediaItem:
    type: str  # "photo" یا "video"
    url: str


@dataclass
class InstagramPost:
    items: list = field(default_factory=list)
    caption: Optional[str] = None
    music_title: Optional[str] = None
    music_url: Optional[str] = None


def extract_shortcode(url: str) -> Optional[str]:
    m = re.search(r"instagram\.com/(?:[^/]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else None


def shortcode_to_media_id(shortcode: str) -> Optional[int]:
    """کد کوتاه پست (مثلاً 'Da7TGN1jAvR') معادل base64 با الفبای مخصوص
    اینستاگرامِ شناسه‌ی عددی مدیاست - همون روشی که ابزارهای دانلودر
    اینستاگرام (instaloader و مشابه) هم استفاده می‌کنن."""
    try:
        media_id = 0
        for ch in shortcode:
            media_id = media_id * 64 + _SHORTCODE_ALPHABET.index(ch)
        return media_id
    except ValueError:
        return None


async def check_cookie_health(cookie_file: Optional[str]) -> tuple[bool, str]:
    """
    برخلاف چک سن فایل (که فرض می‌کرد کوکی تازه = سالم - فرض غلطی که عملاً
    رد شد، چون یه کوکی ۲روزه هم می‌تونه بمیره)، این تابع واقعاً با کوکی به
    اینستاگرام درخواست می‌زنه و می‌بینه سشن هنوز معتبره یا نه. اندپوینت
    accounts/current_user فقط وقتی سشن سالمه ۲۰۰ با اطلاعات یوزر برمی‌گردونه؛
    وگرنه ۴۰۰/۴۰۱ یا logged_out=true می‌ده - همون علامتی که باعث fail شدن
    دانلودهای واقعی هم می‌شه، منتها اینجا بدون این‌که منتظر شکایت کاربر بمونیم.

    خروجی: (سالمه؟, توضیح کوتاه فارسی)
    """
    if not cookie_file:
        return True, "کوکی تنظیم نشده (اختیاریه)"
    cookies = _load_cookies(cookie_file)
    if not cookies:
        return False, "فایل کوکی پیدا/خونده نشد"
    present_names = set(cookies.keys()) if hasattr(cookies, "keys") else set()
    has_sessionid = "sessionid" in present_names
    has_csrf = "csrftoken" in present_names
    headers = {
        "User-Agent": USER_AGENT,
        "X-IG-App-ID": APP_ID,
        "Accept": "*/*",
        **_auth_headers(cookies),
    }
    try:
        async with httpx.AsyncClient(
            headers=headers, cookies=cookies, timeout=15.0, follow_redirects=True
        ) as client:
            resp = await client.get("https://i.instagram.com/api/v1/accounts/current_user/?edit=true")
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    if data.get("user"):
                        return True, "سشن معتبره"
                except Exception:
                    pass
                body_snippet = resp.text[:300] if resp.text else ""
                logger.warning(
                    f"🍪 چک سلامت کوکی: پاسخ ۲۰۰ ولی بدون کلید user - "
                    f"sessionid={'دارد' if has_sessionid else 'ندارد'}, "
                    f"csrftoken={'دارد' if has_csrf else 'ندارد'}, بدنه: {body_snippet!r}"
                )
                return False, "پاسخ ۲۰۰ ولی فرمت غیرمنتظره - احتمال logged_out"
            body_snippet = resp.text[:200] if resp.text else ""
            if "checkpoint_required" in body_snippet:
                return False, "checkpoint_required - نیاز به تایید امنیتی/لاگین مجدد از مرورگر"
            if "login_required" in body_snippet or resp.status_code in (400, 401):
                return False, f"کوکی نامعتبره (کد {resp.status_code}) - نیاز به export دوباره"
            return False, f"کد غیرمنتظره {resp.status_code}"
    except Exception as e:
        return False, f"خطا در چک کردن سشن: {e}"


def _load_cookies(cookie_file: Optional[str]) -> Optional[httpx.Cookies]:
    if not cookie_file:
        return None
    try:
        jar = http.cookiejar.MozillaCookieJar()
        jar.load(cookie_file, ignore_discard=True, ignore_expires=True)
        cookies = httpx.Cookies()
        for c in jar:
            cookies.set(c.name, c.value, domain=c.domain, path=c.path)
        return cookies
    except Exception as e:
        logger.warning(f"خواندن کوکی اینستاگرام ناموفق بود: {e}")
        return None


def _auth_headers(cookies: Optional[httpx.Cookies]) -> dict:
    """
    خیلی از endpointهای private API اینستاگرام (از جمله accounts/current_user
    و media/<id>/info که همین‌جا استفاده می‌شن) علاوه بر کوکی، انتظار یه هدر
    X-CSRFToken دارن که مقدارش باید دقیقاً برابر کوکی csrftoken باشه؛ بدون این
    هدر، حتی با کوکی کاملاً معتبر و تازه هم ممکنه اینستاگرام رفتار "انگار
    لاگین نیستی" نشون بده (۲۰۰ ولی بدون داده‌ی واقعی یوزر/مدیا) - این باگ
    قبلاً اینجا بود و باعث می‌شد چک سلامت کوکی همیشه false بده حتی با کوکی تازه.
    """
    headers = {}
    if cookies:
        token = cookies.get("csrftoken")
        if token:
            headers["X-CSRFToken"] = token
    return headers


def _largest(candidates) -> Optional[str]:
    if not candidates:
        return None
    best = max(candidates, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0))
    return best.get("url")


def _extract_music(item: dict):
    clips = item.get("clips_metadata") or {}
    for key in ("music_info", "original_sound_info"):
        info = clips.get(key)
        if not info:
            continue
        asset = info.get("music_asset_info") or {}
        url = asset.get("progressive_download_url") or info.get("progressive_download_url")
        title = asset.get("title") or info.get("original_audio_title") or asset.get("display_artist")
        if url:
            return title, url
    return None, None


def _parse_media_item(item: dict) -> Optional[InstagramMediaItem]:
    if item.get("video_versions"):
        url = _largest(item["video_versions"])
        if url:
            return InstagramMediaItem(type="video", url=url)
    images = ((item.get("image_versions2") or {}).get("candidates")) or []
    url = _largest(images)
    if url:
        return InstagramMediaItem(type="photo", url=url)
    return None


async def _fetch_via_api(media_id: int, cookie_file: Optional[str]) -> Optional[dict]:
    cookies = _load_cookies(cookie_file)
    headers = {
        "User-Agent": USER_AGENT,
        "X-IG-App-ID": APP_ID,
        "Accept": "*/*",
        "X-Requested-With": "XMLHttpRequest",
        "X-ASBD-ID": "129477",
        "Referer": "https://www.instagram.com/",
        **_auth_headers(cookies),
    }
    # قبلاً i.instagram.com بود - این ساب‌دامین API موبایل دیگه برای درخواست‌های
    # بیرونی جواب نمی‌ده و یه صفحه‌ی وب ۴۰۴ معمولی برمی‌گردونه (نه خطای JSON).
    # همون endpoint زیر دامنه‌ی www هنوز کار می‌کنه - این دقیقاً همون مسیریه که
    # خودِ yt-dlp هم برای گرفتن JSON metadata ازش استفاده می‌کنه و موفقه.
    url = f"https://www.instagram.com/api/v1/media/{media_id}/info/"
    async with httpx.AsyncClient(
        headers=headers, cookies=cookies, timeout=20.0, follow_redirects=True
    ) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            # قبلاً اینجا فقط None برمی‌گشت و هیچ اثری تو لاگ نمی‌ذاشت - وقتی
            # هم yt-dlp و هم این API فیل می‌شدن، هیچ سرنخی نداشتیم که این
            # مسیر چرا رد شده (کوکی منقضی؟ چالش امنیتی؟ ریت‌لیمیت؟). حالا
            # کد وضعیت + تیکه‌ای از بدنه‌ی پاسخ رو لاگ می‌کنیم.
            body_snippet = resp.text[:300] if resp.text else ""
            reason = ""
            if "checkpoint_required" in body_snippet:
                reason = " (checkpoint_required - اکانت کوکی نیاز به تایید امنیتی/لاگین مجدد داره)"
            elif "login_required" in body_snippet:
                reason = " (login_required - کوکی منقضی شده یا نامعتبره)"
            elif resp.status_code == 429:
                reason = " (ریت‌لیمیت - درخواست زیاد از این IP/اکانت)"
            logger.warning(
                f"API داخلی اینستاگرام برای media_id={media_id} با کد {resp.status_code} "
                f"رد شد{reason}. بدنه: {body_snippet!r}"
            )
            return None
        try:
            return resp.json()
        except Exception as e:
            # قبلاً اینجا هم فقط None برمی‌گشت بدون هیچ لاگی - یعنی حالت
            # "status 200 ولی بدنه JSON معتبر نیست" (مثلاً صفحه‌ی چالش/کانسنت)
            # کاملاً بی‌صدا از دید ما قایم می‌شد. الان بدنه رو لاگ می‌کنیم.
            body_snippet = resp.text[:300] if resp.text else ""
            checkpoint_hint = ""
            if "update_risky_contactpoint" in str(resp.url) or "update_risky_contactpoint" in body_snippet:
                checkpoint_hint = (
                    " (ریدایرکت به accounts/update_risky_contactpoint - یعنی این کوکی/سشن "
                    "به‌عنوان مشکوک پرچم‌گذاری شده، معمولاً به‌خاطر IP سرور/دیتاسنتر؛ "
                    "این کوکی برای همین موقتاً بی‌فایده‌ست تا از مرورگر واقعی دوباره لاگین بشه)"
                )
            logger.warning(
                f"API داخلی اینستاگرام برای media_id={media_id}: پاسخ ۲۰۰ ولی "
                f"JSON معتبر نیست ({e}){checkpoint_hint}. بدنه: {body_snippet!r}"
            )
            return None


def _extract_og_description_caption(html_text: str) -> Optional[str]:
    """کپشنِ پست رو از og:description استخراج می‌کنه - مسیر HTML fallback قبلاً
    اصلاً این تگ رو نمی‌خوند و کپشن همیشه گم می‌شد، حتی وقتی خودِ عکس/ویدیو
    درست پیدا می‌شد.

    فرمت og:description اینستاگرام همیشه به یه شکل نیست - بعضی‌وقتا
    '... on Instagram: "متن"'ه، بعضی‌وقتا (خصوصاً وقتی پست تاریخ داشته
    باشه) '... on August 18, 2026: "متن"'ه. قبلاً فقط حالت اول رو تشخیص
    می‌داد و تو حالت دوم کل متن خام (شامل تعداد لایک/کامنت) رو به‌جای کپشن
    برمی‌گردوند. الان به‌جای گشتن دنبال کلمه‌ی «Instagram»، مستقیم دنبال
    آخرین جفت گیومهٔ متن (که همیشه دور کپشن واقعی هست، صرف‌نظر از چیزی که
    قبلش میاد) می‌گردیم - این با هر دو فرمت کار می‌کنه."""
    desc_match = re.search(r'<meta property="og:description" content="([^"]*)"', html_text)
    if not desc_match:
        return None
    raw = html_module.unescape(desc_match.group(1)).strip()
    if not raw:
        return None
    quoted_match = re.search(r':\s*[“"]([^”"]*)[”"]\.?\s*$', raw, re.DOTALL)
    if quoted_match:
        caption = quoted_match.group(1).strip()
        return caption or None
    return raw


async def _fetch_via_html_fallback(url: str, cookie_file: Optional[str] = None) -> Optional[InstagramPost]:
    """وقتی API داخلی جواب نده (مثلاً پست محدودشده)، از og:image/og:video خودِ
    صفحه استفاده می‌کنیم. این فقط تک‌آیتم رو می‌گیره (نه کاروسل، نه موزیک).

    بدون کوکی، اینستاگرام به بازدیدکننده‌ی لاگین‌نکرده اغلب صفحه‌ی login-wall
    نشون می‌ده که og:image/og:video نداره - برای همین کوکی رو هم می‌فرستیم تا
    همون صفحه‌ی واقعی پست رندر بشه."""
    cookies = _load_cookies(cookie_file)
    # اینستاگرام og:image/og:video رو فقط برای User-Agentهای شناخته‌شده‌ی
    # کرالر (فیسبوک/توییتر/...) سمت سرور رندر می‌کنه؛ به مرورگر عادی یه
    # شل خامِ React بدون og:tag می‌ده (چون رندرش سمت کلاینت انجام می‌شه).
    # برای همین این‌جا User-Agent بات استفاده می‌کنیم، نه USER_AGENT معمولی.
    headers = {"User-Agent": "facebookexternalhit/1.1", **_auth_headers(cookies)}
    async with httpx.AsyncClient(
        headers=headers, cookies=cookies, timeout=20.0, follow_redirects=True
    ) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            logger.warning(
                f"HTML fallback اینستاگرام برای {url} با کد {resp.status_code} رد شد."
            )
            return None
        html_text = resp.text

    video_match = re.search(r'<meta property="og:video" content="([^"]+)"', html_text)
    caption = _extract_og_description_caption(html_text)
    if video_match:
        return InstagramPost(
            items=[InstagramMediaItem(type="video", url=html_module.unescape(video_match.group(1)))],
            caption=caption,
        )

    image_match = re.search(r'<meta property="og:image" content="([^"]+)"', html_text)
    if image_match:
        return InstagramPost(
            items=[InstagramMediaItem(type="photo", url=html_module.unescape(image_match.group(1)))],
            caption=caption,
        )

    # نه og:video نه og:image پیدا شد - احتمالاً login-wall یا صفحه‌ی چالش.
    # قبلاً اینجا هم بی‌صدا None برمی‌گشت؛ حالا لاگ می‌کنیم تا بفهمیم واقعاً
    # چه صفحه‌ای گرفتیم (کوکی بازم کافی نبوده؟ پست واقعاً حذف/private شده؟)
    logger.warning(
        f"HTML fallback اینستاگرام برای {url}: نه og:video نه og:image پیدا شد. "
        f"شروع صفحه: {html_text[:300]!r}"
    )
    return None


async def fetch_instagram_post(url: str, cookie_file: Optional[str] = None) -> Optional[InstagramPost]:
    """
    عکس/کاروسل/ویدیو + موزیک پس‌زمینه‌ی یه پست اینستاگرام رو مستقیم می‌گیره
    (بدون yt-dlp). برای پست‌های عمومی نیازی به لاگین نیست.

    ترتیب تلاش‌ها: اول کاملاً بدون کوکی (anonymous)، بعد فقط اگه اون تلاش
    شکست خورد، با کوکی. علتش: وقتی کوکیِ لاگین‌شده توسط اینستاگرام «مشکوک»
    تشخیص داده بشه (مثلاً چون از IP یه سرور/دیتاسنتر استفاده می‌شه)، هر
    درخواستی که اون کوکی رو ببره به‌جای داده‌ی واقعی به یه صفحه‌ی چالش
    امنیتی (`accounts/update_risky_contactpoint`) ریدایرکت می‌شه - با کد
    ۲۰۰ ولی بدون هیچ داده‌ی مفیدی، که قبلاً بی‌صدا به شکست کل تابع منجر
    می‌شد. برای پست‌های عمومی (اکثر لینک‌هایی که کاربرا می‌فرستن) اصلاً
    نیازی به لاگین نیست - دقیقاً همون فرضی که yt-dlp/متادیتای این پروژه هم
    از قبل داره و باهاش موفقه (نگاه کن به MetadataExtractor.extract_from_url:
    اول بدون کوکی، بعد با کوکی به‌عنوان تلاش دوم). این تابع هم حالا همون
    الگو رو دنبال می‌کنه تا کوکیِ چالش‌خورده باعث نشه پست‌های کاملاً عمومی
    هم fail بشن.
    """
    shortcode = extract_shortcode(url)
    data = None
    if shortcode:
        media_id = shortcode_to_media_id(shortcode)
        if media_id:
            try:
                data = await _fetch_via_api(media_id, None)
            except Exception as e:
                logger.warning(f"گرفتن اطلاعات پست اینستاگرام از API (anonymous) ناموفق بود: {e}")

            if (not data or not data.get("items")) and cookie_file:
                try:
                    data = await _fetch_via_api(media_id, cookie_file)
                except Exception as e:
                    logger.warning(f"گرفتن اطلاعات پست اینستاگرام از API (با کوکی) ناموفق بود: {e}")

    if not data or not data.get("items"):
        try:
            result = await _fetch_via_html_fallback(url, cookie_file=None)
        except Exception as e:
            logger.warning(f"گرفتن اطلاعات پست اینستاگرام از HTML (anonymous) ناموفق بود: {e}")
            result = None

        if not result and cookie_file:
            try:
                result = await _fetch_via_html_fallback(url, cookie_file=cookie_file)
            except Exception as e:
                logger.warning(f"گرفتن اطلاعات پست اینستاگرام از HTML (با کوکی) ناموفق بود: {e}")
                result = None

        return result

    item = data["items"][0]
    caption_obj = item.get("caption")
    caption = caption_obj.get("text") if caption_obj else None

    items = []
    if item.get("carousel_media"):
        for sub in item["carousel_media"]:
            parsed = _parse_media_item(sub)
            if parsed:
                items.append(parsed)
    else:
        parsed = _parse_media_item(item)
        if parsed:
            items.append(parsed)

    if not items:
        return None

    music_title, music_url = _extract_music(item)

    return InstagramPost(items=items, caption=caption, music_title=music_title, music_url=music_url)
