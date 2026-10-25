"""
شناسایی نوع لینک و سایت پشتیبان
"""
import re
from enum import Enum
from typing import Optional
from urllib.parse import urlparse, parse_qs


class SiteType(str, Enum):
    """نوع سایت پشتیبان"""
    YOUTUBE = "youtube"
    YOUTUBE_MUSIC = "youtube_music"
    YOUTUBE_SHORTS = "youtube_shorts"

    SPOTIFY = "spotify"
    SOUNDCLOUD = "soundcloud"
    APPLE_MUSIC = "apple_music"

    INSTAGRAM = "instagram"
    INSTAGRAM_REEL = "instagram_reel"

    TIKTOK = "tiktok"
    TWITTER = "twitter"
    FACEBOOK = "facebook"

    APARAT = "aparat"
    VIMEO = "vimeo"

    PINTEREST = "pinterest"

    APKPURE = "apkpure"

    GITHUB = "github"

    PIXELDRAIN = "pixeldrain"
    GOFILE = "gofile"
    DROPBOX = "dropbox"
    FILELU = "filelu"
    MEDIAFIRE = "mediafire"
    GOOGLE_DRIVE = "google_drive"
    MEGA = "mega"

    DIRECT_AUDIO = "direct_audio"
    DIRECT_VIDEO = "direct_video"
    DIRECT_FILE = "direct_file"   # فایل‌های اپلیکیشن و آرشیو (apk/exe/zip/...)

    UNKNOWN = "unknown"


class ContentType(str, Enum):
    """نوع محتوا"""
    VIDEO = "video"
    AUDIO = "audio"
    PLAYLIST = "playlist"
    CHANNEL = "channel"
    FILE = "file"


class LinkDetector:
    """شناسایی لینک و سایت"""

    # پسوندهای فایل اپلیکیشن/آرشیو که مستقیم دانلود می‌شن
    APP_EXTENSIONS = (
        '.apk', '.xapk', '.apks',          # اندروید
        '.exe', '.msi', '.msix',             # ویندوز
        '.dmg', '.pkg',                      # مک
        '.ipa',                              # iOS (sideload)
        '.deb', '.rpm', '.snap', '.flatpak', # لینوکس
        '.zip', '.rar', '.7z',               # آرشیو عمومی
        '.tar', '.tar.gz', '.tgz',
        '.tar.bz2', '.tar.xz', '.gz', '.xz',
        '.AppImage',                         # لینوکس پرتابل
    )

    PATTERNS = {
        SiteType.YOUTUBE: [
            r'(?:https?://)?(?:www\.)?(?:youtube\.com|youtu\.be)',
        ],
        SiteType.YOUTUBE_MUSIC: [
            r'(?:https?://)?(?:www\.)?music\.youtube\.com',
        ],
        SiteType.SPOTIFY: [
            r'(?:https?://)?(?:www\.)?spotify\.com',
        ],
        SiteType.SOUNDCLOUD: [
            r'(?:https?://)?(?:www\.)?soundcloud\.com',
        ],
        SiteType.APPLE_MUSIC: [
            r'(?:https?://)?(?:www\.)?music\.apple\.com',
        ],
        SiteType.INSTAGRAM: [
            r'(?:https?://)?(?:www\.)?instagram\.com',
        ],
        SiteType.TIKTOK: [
            r'(?:https?://)?(?:www\.)?tiktok\.com',
            r'(?:https?://)?(?:vm\.)?tiktok\.com',
        ],
        SiteType.TWITTER: [
            r'(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)',
        ],
        SiteType.FACEBOOK: [
            r'(?:https?://)?(?:www\.)?facebook\.com',
        ],
        SiteType.APARAT: [
            r'(?:https?://)?(?:www\.)?aparat\.com',
        ],
        SiteType.VIMEO: [
            r'(?:https?://)?(?:www\.)?vimeo\.com',
        ],
        SiteType.PINTEREST: [
            r'(?:https?://)?(?:[a-z]{2,3}\.)?pinterest\.[a-z.]+',
            r'(?:https?://)?pin\.it',
        ],
        SiteType.APKPURE: [
            r'(?:https?://)?(?:m\.|www\.)?apkpure\.com',
        ],
        SiteType.GITHUB: [
            r'(?:https?://)?(?:www\.)?github\.com',
            r'(?:https?://)?(?:raw\.)?githubusercontent\.com',
            r'(?:https?://)?codeload\.github\.com',
        ],
        SiteType.PIXELDRAIN: [
            r'(?:https?://)?(?:www\.)?pixeldrain\.com',
        ],
        SiteType.GOFILE: [
            r'(?:https?://)?(?:www\.)?gofile\.io',
        ],
        SiteType.DROPBOX: [
            r'(?:https?://)?(?:www\.)?dropbox\.com',
        ],
        SiteType.FILELU: [
            r'(?:https?://)?(?:www\.)?filelu\.com',
            r'(?:https?://)?file\.lu',
        ],
        SiteType.MEDIAFIRE: [
            r'(?:https?://)?(?:www\.)?mediafire\.com',
        ],
        SiteType.GOOGLE_DRIVE: [
            r'(?:https?://)?drive\.google\.com',
        ],
        SiteType.MEGA: [
            r'(?:https?://)?mega\.(?:nz|co\.nz)',
        ],
    }

    # دامنه‌هایی که خودشون host فایل با share-link هستن (نه لینک مستقیم فایل).
    # این‌ها باید قبل از چک پسوند بررسی بشن، چون مسیر لینک‌شون می‌تونه به یه
    # پسوند فایل واقعی (مثل .mp4) ختم بشه ولی خودِ لینک یه صفحه‌ی پیش‌نمایش/API
    # جداگونه‌ست، نه لینک مستقیم قابل GET زدن (مثل dropbox که با dl=0 صفحه‌ی
    # HTML برمی‌گردونه نه فایل). قبلاً این‌جا نبود و لینک‌های dropbox که به
    # .mp4/.zip/... ختم می‌شدن اشتباهی DIRECT_VIDEO/DIRECT_FILE تشخیص داده
    # می‌شدن و بدون تبدیل به لینک دانلود واقعی، مستقیم fetch می‌شدن.
    _CLOUD_HOST_PATTERNS = {
        SiteType.DROPBOX: PATTERNS[SiteType.DROPBOX],
        SiteType.GOFILE: PATTERNS[SiteType.GOFILE],
        SiteType.PIXELDRAIN: PATTERNS[SiteType.PIXELDRAIN],
        SiteType.FILELU: PATTERNS[SiteType.FILELU],
        SiteType.MEDIAFIRE: PATTERNS[SiteType.MEDIAFIRE],
        SiteType.GOOGLE_DRIVE: PATTERNS[SiteType.GOOGLE_DRIVE],
        SiteType.MEGA: PATTERNS[SiteType.MEGA],
    }

    @staticmethod
    def detect_site(url: str) -> SiteType:
        """تشخیص سایت از لینک"""
        url_lower = url.lower().strip()

        # اول دامنه‌های cloud-host (بالاترین اولویت - قبل از چک پسوند)
        for site_type, patterns in LinkDetector._CLOUD_HOST_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, url_lower, re.IGNORECASE):
                    return site_type

        # بعدش پسوند چک کن
        if LinkDetector._is_direct_app_file(url_lower):
            return SiteType.DIRECT_FILE

        if LinkDetector._is_direct_audio(url_lower):
            return SiteType.DIRECT_AUDIO

        if LinkDetector._is_direct_video(url_lower):
            return SiteType.DIRECT_VIDEO

        if 'youtube.com/shorts' in url_lower or 'youtu.be' in url_lower:
            return SiteType.YOUTUBE_SHORTS

        if 'music.youtube.com' in url_lower:
            return SiteType.YOUTUBE_MUSIC

        if 'instagram.com' in url_lower and ('reel' in url_lower or 'reels' in url_lower):
            return SiteType.INSTAGRAM_REEL

        for site_type, patterns in LinkDetector.PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, url_lower, re.IGNORECASE):
                    return site_type

        return SiteType.UNKNOWN

    @staticmethod
    def _is_direct_app_file(url: str) -> bool:
        """آیا لینک مستقیم به فایل اپلیکیشن یا آرشیو است"""
        clean = url.lower().split('?')[0].split('#')[0]
        return any(clean.endswith(ext.lower()) for ext in LinkDetector.APP_EXTENSIONS)

    @staticmethod
    def _is_direct_audio(url: str) -> bool:
        audio_ext = ('.mp3', '.wav', '.flac', '.aac', '.m4a', '.ogg', '.wma', '.opus')
        clean = url.lower().split('?')[0].split('#')[0]
        return clean.endswith(audio_ext)

    @staticmethod
    def _is_direct_video(url: str) -> bool:
        video_ext = ('.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv', '.m3u8', '.ts')
        clean = url.lower().split('?')[0].split('#')[0]
        return clean.endswith(video_ext)

    @staticmethod
    def _is_direct_file(url: str) -> bool:
        """آیا لینک مستقیم به فایل است (صوتی، ویدیویی یا اپلیکیشن)"""
        return (
            LinkDetector._is_direct_audio(url)
            or LinkDetector._is_direct_video(url)
            or LinkDetector._is_direct_app_file(url)
        )

    @staticmethod
    def extract_id(url: str, site_type: SiteType) -> Optional[str]:
        """استخراج شناسه محتوا از لینک"""
        url_lower = url.lower()

        if site_type == SiteType.YOUTUBE or site_type == SiteType.YOUTUBE_SHORTS:
            match = re.search(r'(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)([\w-]+)', url_lower)
            return match.group(1) if match else None

        elif site_type == SiteType.YOUTUBE_MUSIC:
            match = re.search(r'v=([\w-]+)', url_lower)
            return match.group(1) if match else None

        elif site_type == SiteType.SPOTIFY:
            match = re.search(r'spotify\.com/(?:track|playlist|album)/([a-zA-Z0-9]+)', url_lower)
            return match.group(1) if match else None

        elif site_type == SiteType.SOUNDCLOUD:
            return url

        elif site_type == SiteType.INSTAGRAM:
            match = re.search(r'instagram\.com/(?:p|reel)/([a-zA-Z0-9-]+)', url_lower)
            return match.group(1) if match else None

        elif site_type == SiteType.TIKTOK:
            match = re.search(r'(?:tiktok\.com/@\w+/video/|vm\.tiktok\.com/)(\d+)', url_lower)
            return match.group(1) if match else None

        elif site_type == SiteType.APARAT:
            match = re.search(r'aparat\.com/v/([a-zA-Z0-9]+)', url_lower)
            return match.group(1) if match else None

        return None

    @staticmethod
    def is_playlist(url: str, site_type: SiteType) -> bool:
        """آیا لینک برای پلی‌لیست است"""
        url_lower = url.lower()

        if site_type == SiteType.YOUTUBE:
            return 'playlist?list=' in url_lower or 'youtube.com/playlist' in url_lower

        elif site_type == SiteType.SPOTIFY:
            return '/playlist/' in url_lower

        elif site_type == SiteType.SOUNDCLOUD:
            return '/sets/' in url_lower

        elif site_type == SiteType.APPLE_MUSIC:
            return '/playlist/' in url_lower

        return False

    @staticmethod
    def get_content_type(site_type: SiteType, is_playlist: bool = False) -> ContentType:
        """نوع محتوا را تعیین کن"""
        if is_playlist:
            return ContentType.PLAYLIST

        if site_type in (SiteType.YOUTUBE_MUSIC, SiteType.SPOTIFY,
                         SiteType.SOUNDCLOUD, SiteType.APPLE_MUSIC,
                         SiteType.DIRECT_AUDIO):
            return ContentType.AUDIO

        if site_type in (SiteType.YOUTUBE, SiteType.YOUTUBE_SHORTS,
                         SiteType.INSTAGRAM, SiteType.INSTAGRAM_REEL,
                         SiteType.TIKTOK, SiteType.APARAT, SiteType.VIMEO,
                         SiteType.DIRECT_VIDEO):
            return ContentType.VIDEO

        if site_type in (SiteType.GITHUB, SiteType.DIRECT_FILE, SiteType.APKPURE,
                         SiteType.PIXELDRAIN, SiteType.GOFILE, SiteType.DROPBOX,
                         SiteType.FILELU, SiteType.MEDIAFIRE, SiteType.GOOGLE_DRIVE,
                         SiteType.MEGA):
            return ContentType.FILE

        return ContentType.VIDEO

    DISPLAY_INFO = {
        SiteType.YOUTUBE: ("🎬", "یوتیوب"),
        SiteType.YOUTUBE_MUSIC: ("🎵", "یوتیوب میوزیک"),
        SiteType.YOUTUBE_SHORTS: ("🎬", "یوتیوب شورتز"),
        SiteType.SPOTIFY: ("🎧", "اسپاتیفای"),
        SiteType.SOUNDCLOUD: ("☁️", "ساندکلاود"),
        SiteType.APPLE_MUSIC: ("🍎", "اپل میوزیک"),
        SiteType.INSTAGRAM: ("📸", "اینستاگرام"),
        SiteType.INSTAGRAM_REEL: ("🎞", "اینستاگرام ریلز"),
        SiteType.TIKTOK: ("🎶", "تیک‌تاک"),
        SiteType.TWITTER: ("🐦", "توییتر / X"),
        SiteType.FACEBOOK: ("📘", "فیسبوک"),
        SiteType.APARAT: ("📺", "آپارات"),
        SiteType.VIMEO: ("🎥", "ویمیو"),
        SiteType.PINTEREST: ("📌", "پینترست"),
        SiteType.APKPURE: ("🤖", "APKPure"),
        SiteType.GITHUB: ("🐙", "گیت‌هاب"),
        SiteType.PIXELDRAIN: ("💧", "Pixeldrain"),
        SiteType.GOFILE: ("📁", "Gofile"),
        SiteType.DROPBOX: ("📦", "Dropbox"),
        SiteType.FILELU: ("🗂", "FileLu"),
        SiteType.MEDIAFIRE: ("🔥", "MediaFire"),
        SiteType.GOOGLE_DRIVE: ("🇬", "Google Drive"),
        SiteType.MEGA: ("☁️", "MEGA"),
        SiteType.DIRECT_AUDIO: ("🎼", "فایل صوتی مستقیم"),
        SiteType.DIRECT_VIDEO: ("🎞", "فایل ویدیویی مستقیم"),
        SiteType.DIRECT_FILE: ("📦", "فایل مستقیم (APK/EXE/ZIP/...)"),
    }

    @staticmethod
    def get_display_name(url: str, site_type: "SiteType") -> str:
        """
        اسم نمایشی + ایموجی سایت رو برمی‌گردونه.
        اگه سایت جزو لیست شناخته‌شده‌ها نبود (UNKNOWN)، به‌جای رد کردن،
        از خود دامنه‌ی لینک یه اسم عمومی می‌سازه تا 'download all' واقعی باشه.
        """
        info = LinkDetector.DISPLAY_INFO.get(site_type)
        if info:
            emoji, name = info
            return f"{emoji} {name}"

        try:
            parsed = urlparse(url if '://' in url else f"http://{url}")
            domain = parsed.netloc or parsed.path.split('/')[0]
            domain = domain.replace('www.', '')
        except Exception:
            domain = "لینک عمومی"

        return f"🔗 {domain or 'لینک عمومی'}"

    @staticmethod
    def extract_url(text: str) -> Optional[str]:
        """
        فقط خودِ لینک رو از وسط متن پیام بیرون می‌کشه (مثلاً وقتی کاربر یه توضیح
        قبل یا بعد لینک هم می‌نویسه: 'ایول\nhttps://instagram.com/...').
        اگه هیچ لینکی تو متن نبود None برمی‌گرده.
        """
        url_pattern = r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
        match = re.search(url_pattern, text)
        if match:
            return match.group(0).strip().rstrip('.,)\u200c')

        bare_domain_pattern = r'[a-zA-Z0-9-]+(\.[a-zA-Z0-9-]+)+\.[a-zA-Z]{2,}(/\S*)?'
        match = re.search(bare_domain_pattern, text.strip())
        if match:
            return match.group(0).strip().rstrip('.,)\u200c')

        return None

    @staticmethod
    def is_valid_url(text: str) -> bool:
        """آیا متن یک لینک معتبر است"""
        url_pattern = r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'

        if re.search(url_pattern, text):
            return True

        bare_domain_pattern = r'^[a-zA-Z0-9-]+(\.[a-zA-Z0-9-]+)+\.[a-zA-Z]{2,}(/\S*)?$'
        if re.match(bare_domain_pattern, text.strip()):
            return True

        return False


def detect_link(text: str) -> tuple[SiteType, ContentType]:
    """
    شناسایی سایت و نوع محتوا

    بازگشت: (SiteType, ContentType)
    """
    detector = LinkDetector()

    site = detector.detect_site(text)
    is_pl = detector.is_playlist(text, site)
    content = detector.get_content_type(site, is_pl)

    return site, content
