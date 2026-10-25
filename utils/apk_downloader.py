"""
دانلود APK از صفحه‌ی اپ در APKPure.

⚠️ نکته‌ی مهم: APKPure یه API رسمی برای این کار نداره، پس این ماژول لینک مستقیم
دانلود رو با پارس کردن HTML خودِ صفحه پیدا می‌کنه. اگه APKPure ساختار صفحه‌ش رو
عوض کنه، ممکنه این کد از کار بیفته و الگوهای regex زیر نیاز به آپدیت پیدا کنن.
"""
import re
import httpx
from typing import Optional
from dataclasses import dataclass


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class ApkInfo:
    title: str
    download_url: str


async def resolve_apkpure_download(page_url: str) -> Optional[ApkInfo]:
    """
    از روی لینک صفحه‌ی اپ تو apkpure.com (مثل apkpure.com/whatsapp/com.whatsapp)،
    لینک مستقیم دانلود فایل APK رو پیدا می‌کنه.
    اگه پیدا نشه None برمی‌گردونه (نه Exception، چون این یه حالت قابل‌انتظاره وقتی
    ساختار صفحه فرق کرده یا اپ حذف شده).
    """
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0, headers=_HEADERS) as client:
        resp = await client.get(page_url)
        resp.raise_for_status()
        html = resp.text

    # عنوان اپ (برای caption)
    title_match = re.search(r'<title>([^<]+)</title>', html)
    title = title_match.group(1).split(" - ")[0].strip() if title_match else "اپلیکیشن"

    # چندتا الگوی مختلف امتحان می‌کنیم چون APKPure گاهی ساختار صفحه رو عوض می‌کنه
    patterns = [
        r'id=["\']download_link["\'][^>]*href=["\']([^"\']+)["\']',
        r'href=["\'](https://download\.apkpure\.com/[^"\']+)["\']',
        r'"downloadUrl"\s*:\s*"([^"]+)"',
        r'href=["\'](https://[a-z0-9.-]*apkpure[a-z0-9.-]*/[^"\']+\.(?:apk|xapk)[^"\']*)["\']',
    ]

    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            url = match.group(1).replace("\\/", "/").replace("&amp;", "&")
            return ApkInfo(title=title, download_url=url)

    return None
