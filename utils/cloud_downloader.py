"""
دانلود از سایت‌های هاست فایل عمومی (cloud file hosting) که خودشون ویدیو/موزیک
نیستن، فقط فایل خام میزبانی می‌کنن. این سایت‌ها API رسمی/عمومی دارن (برخلاف
APKPure که فقط پارس HTML بود)، برای همین هر کدوم منطق خودشون رو دارن.

⚠️ نکته‌ی مهم درباره‌ی gofile: این سرویس یه "websiteToken" لازم داره که توی
فایل جاوااسکریپت خودِ سایت (gofile.io/dist/js/global.js) هاردکد شده و گاهی
عوض می‌شه. اینجا یه مقدار fallback گذاشتیم ولی اگه gofile قطع شد، این اولین
جایی‌ه که باید چک کنی (لاگ WARNING مربوطه رو می‌بینی).
"""
import re
import httpx
from typing import Optional, List
from dataclasses import dataclass, field


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# مقدار fallback فعلی websiteToken گوفایل (استخراج‌شده از global.js در زمان نوشتن
# این کد). اگه gofile خطای auth داد، این مقدار احتمالاً منقضی شده.
_GOFILE_WT_FALLBACK = "4fd6sg89d7s6"


@dataclass
class CloudFile:
    title: str
    download_url: str
    # هدرهای اضافی که هنگام GET زدن به download_url لازمه (مثلاً Authorization
    # برای gofile). _download_generic_file باید این‌ها رو هم پاس بده.
    extra_headers: dict = field(default_factory=dict)
    size: Optional[int] = None


# ---------------------------------------------------------------------------
# Pixeldrain
# ---------------------------------------------------------------------------

_PIXELDRAIN_ID_RE = re.compile(r'pixeldrain\.com/(?:u|l|api/file)/([a-zA-Z0-9]+)', re.IGNORECASE)


async def resolve_pixeldrain(url: str) -> Optional[CloudFile]:
    """
    pixeldrain.com/u/{id} -> فایل تکی
    API رسمی و ساده داره، نیاز به هیچ توکنی نداره برای فایل‌های عمومی.
    """
    match = _PIXELDRAIN_ID_RE.search(url)
    if not match:
        return None
    file_id = match.group(1)

    async with httpx.AsyncClient(timeout=20.0, headers=_HEADERS) as client:
        resp = await client.get(f"https://pixeldrain.com/api/file/{file_id}/info")
        if resp.status_code != 200:
            return None
        data = resp.json()

    if data.get("success") is False or "name" not in data:
        return None

    title = data.get("name") or file_id
    size = data.get("size")
    download_url = f"https://pixeldrain.com/api/file/{file_id}?download"
    return CloudFile(title=title, download_url=download_url, size=size)


# ---------------------------------------------------------------------------
# Dropbox
# ---------------------------------------------------------------------------

def resolve_dropbox(url: str) -> Optional[CloudFile]:
    """
    لینک‌های اشتراک‌گذاری Dropbox (www.dropbox.com/s/... یا /scl/fi/...) رو
    به لینک دانلود مستقیم تبدیل می‌کنه. نیازی به درخواست شبکه نیست - فقط
    پارامتر dl رو 1 می‌کنیم (Dropbox خودش این‌طوری صفحه‌ی پیش‌نمایش رو رد
    می‌کنه و مستقیم فایل رو serve می‌کنه).
    """
    if "dropbox.com" not in url.lower():
        return None

    # اسم فایل از انتهای مسیر (قبل از query string)
    path_part = url.split("?")[0]
    filename = path_part.rstrip("/").rsplit("/", 1)[-1] or "dropbox_file"

    # هر مقدار قبلی dl=... رو حذف کن و dl=1 بذار
    base = re.sub(r'([?&])dl=\d', '', url)
    separator = "&" if "?" in base else "?"
    download_url = f"{base}{separator}dl=1"

    return CloudFile(title=filename, download_url=download_url)


# ---------------------------------------------------------------------------
# Gofile
# ---------------------------------------------------------------------------

_GOFILE_ID_RE = re.compile(r'gofile\.io/d/([a-zA-Z0-9]+)', re.IGNORECASE)


async def _gofile_get_website_token(client: httpx.AsyncClient) -> str:
    """سعی می‌کنه websiteToken فعلی رو از global.js خود سایت بیرون بکشه."""
    try:
        resp = await client.get("https://gofile.io/dist/js/global.js")
        if resp.status_code == 200:
            match = re.search(r'appdata\.wt\s*=\s*"([^"]+)"', resp.text)
            if match:
                return match.group(1)
    except Exception:
        pass
    return _GOFILE_WT_FALLBACK


async def resolve_gofile(url: str) -> Optional[List[CloudFile]]:
    """
    gofile.io/d/{id} می‌تونه یه پوشه با چندتا فایل باشه (نه همیشه یه فایل تکی)،
    برای همین لیست برمی‌گردونه. اگه پوشه پسورد داشته باشه یا API عوض شده باشه
    None برمی‌گردونه.
    """
    match = _GOFILE_ID_RE.search(url)
    if not match:
        return None
    content_id = match.group(1)

    async with httpx.AsyncClient(timeout=20.0, headers=_HEADERS) as client:
        # ۱. اکانت مهمان بگیر (رایگان، بدون لاگین)
        acc_resp = await client.post("https://api.gofile.io/accounts")
        if acc_resp.status_code != 200:
            return None
        acc_data = acc_resp.json()
        if acc_data.get("status") != "ok":
            return None
        token = acc_data["data"]["token"]

        # ۲. websiteToken لازم برای خوندن محتوا
        wt = await _gofile_get_website_token(client)

        # ۳. محتوای پوشه/فایل رو بگیر
        content_resp = await client.get(
            f"https://api.gofile.io/contents/{content_id}",
            params={"wt": wt},
            headers={"Authorization": f"Bearer {token}"},
        )
        if content_resp.status_code != 200:
            return None
        content_data = content_resp.json()
        if content_data.get("status") != "ok":
            # معمولاً یعنی پوشه پسورد داره یا wt منقضی شده
            return None

    node = content_data["data"]
    auth_header = {"Cookie": f"accountToken={token}"}

    files: List[CloudFile] = []
    children = node.get("children") or {}
    if children:
        for child in children.values():
            if child.get("type") != "file":
                continue
            link = child.get("link")
            if not link:
                continue
            files.append(CloudFile(
                title=child.get("name") or content_id,
                download_url=link,
                extra_headers=auth_header,
                size=child.get("size"),
            ))
    elif node.get("type") == "file" and node.get("link"):
        files.append(CloudFile(
            title=node.get("name") or content_id,
            download_url=node["link"],
            extra_headers=auth_header,
            size=node.get("size"),
        ))

    return files or None


# ---------------------------------------------------------------------------
# FileLu
# ---------------------------------------------------------------------------

_FILELU_ID_RE = re.compile(r'file(?:lu\.com|\.lu/d)/([a-zA-Z0-9]+)', re.IGNORECASE)


async def resolve_filelu(url: str, api_key: Optional[str]) -> Optional[CloudFile]:
    """
    filelu.com/{code} یا file.lu/d/{code}. برخلاف بقیه، دانلود مستقیم رایگان
    از خودِ سایت بسته‌ست (پیام "Upgrade for Fast Download" رو صفحه نشون
    می‌ده)، پس فقط از طریق API رسمی اکانت (کلید API) کار می‌کنه:
    https://filelu.com/pages/api
    """
    if not api_key:
        return None

    match = _FILELU_ID_RE.search(url)
    if not match:
        return None
    file_code = match.group(1)

    async with httpx.AsyncClient(timeout=20.0, headers=_HEADERS) as client:
        resp = await client.post(
            "https://filelu.com/api/file/direct_link",
            data={"file_code": file_code, "key": api_key},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data.get("status") != 200 or not data.get("result", {}).get("url"):
            return None
        direct_url = data["result"]["url"]
        size = data["result"].get("size")

        title = file_code
        info_resp = await client.get(
            "https://filelu.com/api/file/info",
            params={"file_code": file_code, "key": api_key},
        )
        if info_resp.status_code == 200:
            info_data = info_resp.json()
            result = info_data.get("result") or []
            if result and result[0].get("name"):
                title = result[0]["name"]

    return CloudFile(title=title, download_url=direct_url, size=size)


# ---------------------------------------------------------------------------
# MediaFire
# ---------------------------------------------------------------------------

_MEDIAFIRE_RE = re.compile(r'mediafire\.com/(?:file|download)/([a-zA-Z0-9]+)', re.IGNORECASE)


async def resolve_mediafire(url: str) -> Optional[CloudFile]:
    """
    mediafire.com/file/{id}/... - صفحه‌ی خودِ فایل یه لینک دانلود مستقیم
    (روی دامنه‌ی CDN مثل download123.mediafire.com) تو تگ
    <a id="downloadButton" href="..."> داره. API رسمی عمومی نداره، پس مثل
    apk_downloader فقط HTML رو پارس می‌کنیم.
    """
    if not _MEDIAFIRE_RE.search(url):
        return None

    async with httpx.AsyncClient(follow_redirects=True, timeout=20.0, headers=_HEADERS) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            return None
        html_text = resp.text

    match = re.search(r'id="downloadButton"[^>]*href="([^"]+)"', html_text)
    if not match:
        # فرمت جایگزین که بعضی صفحات مدیافایر دارن
        match = re.search(r'href="(https://download[^"]*mediafire\.com/[^"]+)"', html_text)
    if not match:
        return None
    download_url = match.group(1).replace("&amp;", "&")

    title_match = re.search(r'class="dl-btn-label"[^>]*title="([^"]+)"', html_text)
    if not title_match:
        title_match = re.search(r'<title>([^<]+)</title>', html_text)
    title = title_match.group(1).strip() if title_match else download_url.rsplit("/", 1)[-1]

    return CloudFile(title=title, download_url=download_url)


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------

_GDRIVE_ID_PATTERNS = [
    re.compile(r'drive\.google\.com/file/d/([a-zA-Z0-9_-]+)'),
    re.compile(r'drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)'),
    re.compile(r'drive\.google\.com/uc\?(?:export=download&)?id=([a-zA-Z0-9_-]+)'),
    re.compile(r'[?&]id=([a-zA-Z0-9_-]+)'),
]


async def resolve_google_drive(url: str) -> Optional[CloudFile]:
    """
    فایل‌های عمومیِ share-شده‌ی Google Drive. برای فایل‌های کوچیک یه GET ساده
    با confirm=t کافیه؛ برای فایل‌های بزرگ (که گوگل قبلش صفحه‌ی «امکان اسکن
    ویروس نبود» نشون می‌ده) هم confirm=t معمولاً رد می‌شه. اگه فایل private
    باشه یا محدودیت quota دانلود عمومی خورده باشه (خطای معروف گوگل درایو
    برای فایل‌های پربازدید)، None برمی‌گردونه.
    """
    file_id = None
    for pattern in _GDRIVE_ID_PATTERNS:
        m = pattern.search(url)
        if m:
            file_id = m.group(1)
            break
    if not file_id:
        return None

    download_url = f"https://drive.google.com/uc?export=download&confirm=t&id={file_id}"

    # اسم فایل رو از صفحه‌ی متادیتای عمومی گوگل درایو می‌گیریم (اختیاریه،
    # اگه نشد از filename پیش‌فرض تو _download_generic_file استفاده می‌شه)
    title = None
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0, headers=_HEADERS) as client:
            resp = await client.get(f"https://drive.google.com/file/d/{file_id}/view")
            if resp.status_code == 200:
                m = re.search(r'<meta property="og:title" content="([^"]+)"', resp.text)
                if m:
                    title = m.group(1).strip()
    except Exception:
        pass

    return CloudFile(title=title or file_id, download_url=download_url)


# ---------------------------------------------------------------------------
# MEGA
# ---------------------------------------------------------------------------
# MEGA رمزنگاری end-to-end داره (کلید رمزگشایی تو خودِ #! فرگمنت URL میاد)،
# پیاده‌سازیش با httpx خام عملاً یعنی بازنویسی بخشی از پروتکل MEGA. به‌جاش
# از کتابخونه‌ی mega.py استفاده می‌کنیم (باید تو requirements.txt باشه:
# pip install mega.py). این کتابخونه sync/blocking-ه، برای همین با
# asyncio.to_thread صداش می‌زنیم تا event loop رو قفل نکنه.

_MEGA_URL_RE = re.compile(r'mega\.(?:nz|co\.nz)/(?:file|folder)?/?', re.IGNORECASE)


async def download_mega_file(url: str, dest_dir: str) -> Optional["CloudFile"]:
    """
    برخلاف بقیه‌ی resolver ها (که فقط لینک مستقیم برمی‌گردونن و دانلود واقعی
    با _download_generic_file/httpx انجام می‌شه)، MEGA همچین لینک مستقیمی
    نداره - فایل رمزنگاری‌شده‌ست و mega.py خودش هم‌زمان دانلود و رمزگشایی
    می‌کنه. برای همین این تابع مستقیماً فایل رو تو dest_dir دانلود می‌کنه و
    مسیر نهایی رو برمی‌گردونه (نه یه URL).
    """
    import asyncio

    if not _MEGA_URL_RE.search(url):
        return None

    def _sync_download():
        from mega import Mega  # type: ignore
        m = Mega()
        client = m.login()  # اکانت مهمان/anonymous - برای لینک‌های عمومی کافیه
        return client.download_url(url, dest_path=dest_dir)

    try:
        file_path = await asyncio.to_thread(_sync_download)
    except Exception:
        return None

    if not file_path:
        return None

    return CloudFile(title=str(file_path).rsplit("/", 1)[-1], download_url=str(file_path))
