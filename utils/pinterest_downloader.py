"""
دانلود مستقیم عکس یا ویدیوی پین از پینترست، بدون yt-dlp.

چرا این ماژول لازم بود؟
۱. اکسترکتور پینترست تو yt-dlp پین‌های عکسی رو اصلاً پشتیبانی نمی‌کنه (فقط
   ویدیو) - یه محدودیت شناخته‌شده و عمدیه، نه باگ قابل فیکس با تنظیمات.
۲. حتی برای پین‌های ویدیویی هم این‌روزها گاهی با ۴۰۴ تقلبی از سمت پینترست
   مواجه می‌شه (احتمالاً محدودیت ضدبات).

برای همین این ماژول مستقیم لینک عکس/ویدیوی واقعیِ پین رو از API داخلی
پینترست (همون‌ که خودِ سایت pinterest.com استفاده می‌کنه) می‌گیره و اگه اون
جواب نداد، به‌عنوان fallback از og:image/og:video خودِ صفحه استفاده می‌کنه.
تشخیص عکس بودن یا ویدیو بودن، همیشه بر اساس داده‌ی واقعی پین انجام می‌شه، نه
فرض ثابت.
"""
import re
import json
import html as html_module
from dataclasses import dataclass
from typing import Optional

import httpx
from loguru import logger

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class PinterestMedia:
    type: str  # "photo" یا "video"
    url: str
    title: Optional[str] = None
    is_hls: bool = False  # ویدیوهای Story Pin/Idea Pin به‌صورت HLS (.m3u8) هستن، نه mp4 مستقیم


def extract_pin_id(url: str) -> Optional[str]:
    m = re.search(r"pinterest\.[a-z.]+/pin/(\d+)", url)
    return m.group(1) if m else None


def _best_from_size_map(size_map: dict) -> Optional[str]:
    """بین سایزهای مختلف تو دیکشنری (مثل images یا video_list)، بزرگ‌ترین
    رزولوشن رو انتخاب می‌کنه."""
    if not isinstance(size_map, dict):
        return None
    best_url, best_area = None, -1
    for val in size_map.values():
        if not isinstance(val, dict):
            continue
        url = val.get("url")
        if not url:
            continue
        area = (val.get("width") or 0) * (val.get("height") or 0)
        if area > best_area:
            best_area, best_url = area, url
    return best_url


async def _fetch_via_resource_api(pin_id: str, pin_url: str) -> Optional[PinterestMedia]:
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": pin_url,
        "Accept": "application/json, text/javascript, */*, q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        # "www/pin/[id].js" دیگه هندلری نداره که پینترست قبول کنه؛ این یکی
        # همونیه که خودِ وب‌کلاینت pinterest.com الان می‌فرسته.
        "X-Pinterest-PWS-Handler": "www/[username].js",
    }
    # نکته: پینترست مرتب رفتار field_set_key رو عوض می‌کنه. یه زمانی
    # "detailed" برای درخواست بدون لاگین ۴۰۳ می‌داد و فقط "unauth_react_main_pin"
    # جواب می‌داد؛ الان برعکسش دیده شده - "unauth_react_main_pin" با ۲۰۰ جواب
    # می‌ده ولی دیگه کلید "videos" رو اصلاً برنمی‌گردونه (حتی برای پین‌های
    # ویدیویی)، در حالی که "detailed" هنوز داده‌ی کامل ویدیو رو می‌ده. برای
    # اینکه با تغییرات بعدی پینترست هم خراب نشه، هر دو رو به‌ترتیب امتحان
    # می‌کنیم و اولین جوابی که واقعاً داده‌ی قابل‌استفاده (ویدیو یا عکس) داشته
    # باشه رو قبول می‌کنیم.
    field_set_keys = ("detailed", "unauth_react_main_pin")

    async with httpx.AsyncClient(headers=headers, timeout=20.0, follow_redirects=True) as client:
        for field_set_key in field_set_keys:
            options = {"id": pin_id, "field_set_key": field_set_key}
            params = {"data": json.dumps({"options": options})}
            resp = await client.get("https://www.pinterest.com/resource/PinResource/get/", params=params)
            if resp.status_code != 200:
                logger.warning(
                    f"Pinterest resource API (field_set_key={field_set_key}) کد "
                    f"{resp.status_code} داد برای pin {pin_id}: {resp.text[:300]}"
                )
                continue
            try:
                payload = resp.json()
            except Exception as e:
                logger.warning(
                    f"جواب resource API پینترست (field_set_key={field_set_key}) برای pin "
                    f"{pin_id} JSON نبود: {e} | body: {resp.text[:300]}"
                )
                continue

            data = (((payload or {}).get("resource_response") or {}).get("data")) or {}
            if not data:
                logger.warning(
                    f"resource API پینترست (field_set_key={field_set_key}) برای pin {pin_id} "
                    f"داده‌ای برنگردوند: {str(payload)[:300]}"
                )
                continue

            media = _parse_pin_data(data, pin_id, field_set_key)
            if media:
                return media
            # این field_set_key جواب داد ولی نه ویدیو نه عکس توش بود - قبل از
            # تسلیم شدن، field_set_key بعدی رو هم امتحان می‌کنیم

    return None


def _extract_story_pin_video(story_pin_data: dict) -> Optional[str]:
    """
    پین‌های Story Pin/Idea Pin ویدیوشون رو زیر کلید بالاسطحیِ 'videos' نمی‌ذارن؛
    داخل story_pin_data.pages[].video.video_list (یا زیر هر کدوم از blocks
    اون صفحه) قایم شده و همیشه یه لینک HLS (.m3u8) هست، نه mp4 مستقیم.
    """
    if not isinstance(story_pin_data, dict):
        return None
    for page in story_pin_data.get("pages") or []:
        if not isinstance(page, dict):
            continue
        video_list = ((page.get("video") or {}).get("video_list") or {})
        url = _best_from_size_map(video_list)
        if url:
            return url
        # بعضی صفحه‌ها ویدیو رو مستقیم رو page نمی‌ذارن، داخل یکی از blocks
        for block in page.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            block_video_list = ((block.get("video") or {}).get("video_list") or {})
            url = _best_from_size_map(block_video_list)
            if url:
                return url
    return None


def _parse_pin_data(data: dict, pin_id: str, field_set_key: str) -> Optional["PinterestMedia"]:
    title = data.get("title") or data.get("grid_title")

    videos = data.get("videos")
    if isinstance(videos, dict):
        video_url = _best_from_size_map(videos.get("video_list"))
        if video_url:
            return PinterestMedia(type="video", url=video_url, title=title)
        # اگه کلید videos هست ولی url ای ازش درنیومد، یعنی یا video_list خالیه یا
        # ساختار داخلیش عوض شده - این حالت رو جدا از "پین اصلاً ویدیو نداره" لاگ
        # می‌کنیم تا اگه پینترست دوباره فرمت رو عوض کرد، سریع بفهمیم.
        logger.warning(
            f"pin {pin_id} (field_set_key={field_set_key}): کلید 'videos' هست ولی لینکی "
            f"ازش استخراج نشد. محتوای videos: {json.dumps(videos)[:500]}"
        )
    elif videos is not None:
        # videos هست ولی dict نیست (مثلاً لیست یا رشته) - یعنی پینترست فرمتش رو
        # عوض کرده و _best_from_size_map اصلاً باهاش کار نمی‌کنه
        logger.warning(
            f"pin {pin_id} (field_set_key={field_set_key}): کلید 'videos' یه "
            f"{type(videos).__name__} هست، نه dict. مقدار: {str(videos)[:500]}"
        )

    # پین ویدیویی معمولی نبود (videos خالی/null) - قبل از نتیجه‌گیری «عکسه»،
    # چک می‌کنیم که پین Story Pin/Idea Pin نباشه (اون‌ها ویدیوشون رو جای
    # دیگه‌ای می‌ذارن، نه زیر 'videos')
    story_video_url = _extract_story_pin_video(data.get("story_pin_data"))
    if story_video_url:
        logger.info(f"pin {pin_id} (field_set_key={field_set_key}): ویدیوی HLS از story_pin_data پیدا شد")
        return PinterestMedia(type="video", url=story_video_url, title=title, is_hls=True)

    if videos is None and not story_video_url:
        logger.info(
            f"pin {pin_id} (field_set_key={field_set_key}): نه 'videos' و نه ویدیوی "
            f"story_pin_data پیدا شد"
        )

    image_url = _best_from_size_map(data.get("images"))
    if image_url:
        return PinterestMedia(type="photo", url=image_url, title=title)

    logger.warning(
        f"pin {pin_id} (field_set_key={field_set_key}): نه ویدیو نه عکس تو داده‌ی "
        f"resource API پیدا شد. کلیدهای موجود: {list(data.keys())}"
    )
    return None


async def _fetch_via_html_fallback(pin_url: str) -> Optional[PinterestMedia]:
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(headers=headers, timeout=20.0, follow_redirects=True) as client:
        resp = await client.get(pin_url)
        if resp.status_code != 200:
            logger.warning(f"صفحه‌ی پین پینترست ({pin_url}) کد {resp.status_code} داد")
            return None
        html_text = resp.text

    video_match = (
        re.search(r'<meta property="og:video:secure_url" content="([^"]+)"', html_text)
        or re.search(r'<meta property="og:video" content="([^"]+)"', html_text)
    )
    if video_match:
        logger.info(f"pin ({pin_url}): ویدیو از og:video تو HTML fallback پیدا شد")
        return PinterestMedia(type="video", url=html_module.unescape(video_match.group(1)))

    image_match = re.search(r'<meta property="og:image" content="([^"]+)"', html_text)
    if image_match:
        logger.info(
            f"pin ({pin_url}): هیچ og:video تو HTML نبود، فقط og:image (تامبنیل) پیدا شد - "
            f"اگه این پین واقعاً ویدیوئه، یعنی پینترست دیگه og:video رو تو HTML نمی‌ذاره"
        )
        return PinterestMedia(type="photo", url=html_module.unescape(image_match.group(1)))

    # اگه نه og:video پیدا شد نه og:image، احتمالاً پینترست به‌جای صفحه‌ی
    # واقعیِ پین یه صفحه‌ی لاگین/چالش ضدبات برگردونده - این علامت‌ها رو چک
    # می‌کنیم تا تو لاگ مشخص باشه فرق این حالت با "پین واقعاً چیزی نداره".
    looks_blocked = any(
        marker in html_text.lower()
        for marker in ("id=\"login", "class=\"loginform", "verify you", "unusual traffic", "captcha")
    )
    logger.warning(
        f"هیچ og:image/og:video تو صفحه‌ی پین پیدا نشد ({pin_url})"
        + (" - به‌نظر صفحه‌ی لاگین/چالش ضدبات بوده" if looks_blocked else f" | طول HTML: {len(html_text)}")
    )
    return None


async def fetch_pinterest_pin(url: str) -> Optional[PinterestMedia]:
    """
    عکس یا ویدیوی واقعیِ یه پین پینترست رو مستقیم می‌گیره (بدون yt-dlp)، و
    درست تشخیص می‌ده که پین عکسه یا ویدیو - بر اساس داده‌ی واقعیِ پین، نه
    یه فرض ثابت.
    """
    resolved_url = url
    pin_id = extract_pin_id(url)

    if not pin_id:
        # لینک کوتاه (pin.it/...) - باید ریدایرکت بشه تا لینک اصلیِ پین به‌دست بیاد
        try:
            async with httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=15.0
            ) as client:
                resp = await client.get(url)
                resolved_url = str(resp.url)
                pin_id = extract_pin_id(resolved_url)
        except Exception as e:
            logger.warning(f"ریزالو کردن لینک کوتاه پینترست ناموفق بود: {e}")

    if pin_id:
        try:
            media = await _fetch_via_resource_api(pin_id, resolved_url)
            if media:
                logger.info(f"pin {pin_id}: از resource API گرفته شد (نوع: {media.type})")
                return media
        except Exception as e:
            logger.warning(f"گرفتن اطلاعات پین از API پینترست ناموفق بود: {e}")

    try:
        fallback_media = await _fetch_via_html_fallback(resolved_url)
        if fallback_media:
            logger.info(f"pin ({resolved_url}): از HTML fallback گرفته شد (نوع: {fallback_media.type})")
        return fallback_media
    except Exception as e:
        logger.warning(f"گرفتن اطلاعات پین از HTML ناموفق بود: {e}")
        return None
