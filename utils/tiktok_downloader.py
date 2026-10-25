"""
دانلود ویدیو/صدای TikTok از طریق API عمومی tikwm.com، به‌عنوان fallback
وقتی اکسترکتور وب یت-دی‌ال‌پی fail می‌شه.

چرا این ماژول لازم شد؟
TikTok مرتب صفحه‌ی وب و مکانیزم anti-bot/challenge‌ش رو عوض می‌کنه، و هر بار
که این اتفاق می‌افته، اکسترکتور وب یت-دی‌ال‌پی (`_solve_challenge_and_set_cookies`
در tiktok.py) می‌شکنه تا خود پروژه‌ی یت-دی‌ال‌پی یه پچ جدید بذاره - این معمولاً
چند روز طول می‌کشه (نگاه کن به ایشوهای gh.com/yt-dlp/yt-dlp با برچسب
site-bug روی tiktok.py). این وقفه‌ها ربطی به سرور/کوکی/نسخه‌ی ما ندارن.

tikwm.com یه سرویس واسط عمومیه که خودش مستقیم از API داخلی اپ موبایل
TikTok می‌خونه (نه صفحه‌ی وب)، پس این مسیر challenge اصلاً درگیرش نمی‌کنه.
چون یه سرویس شخص‌ثالثه (نه یت-دی‌ال‌پی)، هم می‌تونه هر لحظه از کار بیفته و
هم لینک‌های واترمارک‌دار/بدون‌واترمارک برمی‌گردونه - برای همین به‌عنوان
fallback استفاده می‌شه، نه جایگزین همیشگی یت-دی‌ال‌پی.
"""
import re
from dataclasses import dataclass
from typing import Optional

import httpx
from loguru import logger

API_URL = "https://www.tikwm.com/api/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class TikTokMedia:
    video_url: Optional[str]  # بدون واترمارک، کیفیت HD در صورت وجود
    audio_url: Optional[str]  # فقط موزیک/صدای پس‌زمینه
    title: Optional[str]
    cover_url: Optional[str]
    author: Optional[str]
    duration: Optional[int]
    is_image_post: bool = False  # اسلایدشوی عکسی TikTok (بدون ویدیوی واقعی)


def _looks_like_tiktok_url(url: str) -> bool:
    return bool(re.search(r"tiktok\.com|vt\.tiktok|vm\.tiktok", url, re.IGNORECASE))


async def fetch_tiktok_video(url: str) -> Optional[TikTokMedia]:
    """
    از API عمومی tikwm.com اطلاعات و لینک دانلود ویدیو/صدای یه پست TikTok
    رو می‌گیره. اگه چیزی پیدا نشه یا سرویس جواب ندی، None برمی‌گردونه (نه
    exception) تا caller بتونه ساده fallback رو تشخیص بده.
    """
    if not _looks_like_tiktok_url(url):
        return None

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(API_URL, params={"url": url, "hd": "1"}, headers=headers)
            resp.raise_for_status()
            payload = resp.json()
    except Exception as e:
        logger.warning(f"درخواست به tikwm.com برای {url} ناموفق بود: {e}")
        return None

    if payload.get("code") != 0:
        logger.warning(f"tikwm.com برای {url} پاسخ ناموفق داد: {payload.get('msg')}")
        return None

    data = payload.get("data") or {}
    # ترتیب اولویت: hdplay (بدون واترمارک، HD) > play (بدون واترمارک، معمولی)
    video_url = data.get("hdplay") or data.get("play")
    audio_url = data.get("music")
    images = data.get("images") or []  # پست‌های اسلایدشو (فقط عکس)، اگه باشه

    if not video_url and not images:
        return None

    author_info = data.get("author") or {}

    return TikTokMedia(
        video_url=video_url,
        audio_url=audio_url,
        title=data.get("title") or None,
        cover_url=data.get("cover") or data.get("origin_cover"),
        author=author_info.get("unique_id") or author_info.get("nickname"),
        duration=data.get("duration"),
        is_image_post=bool(images) and not video_url,
    )
