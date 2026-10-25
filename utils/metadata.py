"""
استخراج metadata از فایل‌ها و لینک‌ها
"""
import json
import asyncio
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass
from datetime import timedelta

from loguru import logger


# حداکثر تعداد استخراج metadata (yt-dlp) که همزمان اجرا می‌شن.
# بدون این، وقتی چند نفر همزمان لینک بفرستن (مثلاً ۱۰ نفر تو ۵ ثانیه)، همه‌شون
# باهم پردازش سنگین yt-dlp رو شروع می‌کنن و CPU/شبکه‌ی سرور رو کاملاً اشغال می‌کنن.
_METADATA_SEMAPHORE = asyncio.Semaphore(5)


@dataclass
class MediaMetadata:
    """متادیتای رسانه"""
    title: str
    artist: Optional[str] = None
    album: Optional[str] = None
    duration: Optional[int] = None  # ثانیه
    duration_formatted: Optional[str] = None
    genre: Optional[str] = None
    year: Optional[int] = None
    thumbnail_url: Optional[str] = None
    description: Optional[str] = None  # کپشن/توضیحات کامل پست (اینستاگرام، توییتر و مشابه)
    
    # برای ویدیو
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    
    # خام
    raw_data: Dict[str, Any] = None
    
    def __post_init__(self):
        """پردازش بعد از شروع"""
        if not self.title:
            self.title = "بدون عنوان"
        
        if not self.artist:
            self.artist = "هنرمند نامشخص"
        
        if self.duration and not self.duration_formatted:
            self.duration_formatted = self._format_duration(self.duration)
    
    @staticmethod
    def _format_duration(seconds: int) -> str:
        """تبدیل ثانیه به فرمت hh:mm:ss"""
        td = timedelta(seconds=seconds)
        total_seconds = int(td.total_seconds())
        
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        secs = total_seconds % 60
        
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"


class MetadataExtractor:
    """استخراج metadata با yt-dlp"""

    @staticmethod
    async def extract_from_url(url: str, timeout: int = 30, cookie_file: Optional[str] = None) -> Optional[MediaMetadata]:
        """استخراج metadata از لینک

        نکته: برای اینستاگرام، فرستادن کوکیِ لاگین‌شده باعث می‌شه yt-dlp از یه
        اندپوینت خصوصی دیگه استفاده کنه که این‌روزها اغلب ۴۰۰/۴۰۴ می‌ده، در
        حالی که همون پست بدون کوکی (مسیر anonymous) درست جواب می‌ده. برای
        همین اول بدون کوکی امتحان می‌کنیم و کوکی رو فقط اگه اون تلاش fail شد
        به‌عنوان تلاش دوم می‌فرستیم (همون منطقی که تو worker/tasks.py برای
        خودِ دانلود هم پیاده شده)."""
        result = await MetadataExtractor._run_yt_dlp_dump_json(url, timeout, cookie_file=None)
        if result is None and cookie_file:
            result = await MetadataExtractor._run_yt_dlp_dump_json(url, timeout, cookie_file=cookie_file)
        return result

    @staticmethod
    async def _run_yt_dlp_dump_json(url: str, timeout: int, cookie_file: Optional[str] = None) -> Optional[MediaMetadata]:
        try:
            # استفاده از yt-dlp برای گرفتن اطلاعات
            cmd = [
                'yt-dlp',
                '--dump-json',
                '--no-warnings',
                '--no-playlist',
            ]
            # یوتیوب/یوتیوب‌میوزیک: impersonate باعث گیر کردن/تایم‌اوت می‌شه، پس اعمال نمی‌کنیم
            if not any(h in url for h in ("youtube.com", "youtu.be", "music.youtube.com")):
                cmd += ['--impersonate', 'chrome']

            if cookie_file:
                cmd += ['--cookies', cookie_file]
            
            cmd.append(url)

            # subprocess.run بلاکینگه؛ اگه مستقیم await بشه، کل event loop بات (یعنی
            # جواب‌دادن به همه‌ی کاربرای دیگه) تا وقتی yt-dlp تموم بشه قفل می‌مونه.
            # با to_thread تو یه ترد جدا اجرا می‌شه و event loop آزاد می‌مونه.
            # سمافور هم جلوی اجرای بیش‌ازحدِ همزمانش رو می‌گیره تا سرور زیر بار له نشه.
            async with _METADATA_SEMAPHORE:
                result = await asyncio.to_thread(
                    subprocess.run,
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout
                )
            
            if result.returncode != 0:
                return None
            
            data = json.loads(result.stdout)
            return MetadataExtractor._parse_yt_dlp_info(data)
        
        except Exception as e:
            logger.warning(f"خطا در استخراج metadata: {e}")
            return None
    
    @staticmethod
    def _parse_yt_dlp_info(data: Dict[str, Any]) -> MediaMetadata:
        """تجزیه خروجی yt-dlp"""
        
        # عنوان
        title = data.get('title', 'بدون عنوان')
        
        # هنرمند (از فیلدهای مختلف)
        artist = None
        for key in ['uploader', 'artist', 'creator', 'channel']:
            if data.get(key):
                artist = data.get(key)
                break
        
        # اگه artist خالی باشه
        if not artist or artist.lower() == 'none':
            artist = data.get('uploader', 'هنرمند نامشخص')
        
        # آلبوم
        album = data.get('album', None)
        
        # مدت زمان - yt-dlp گاهی float برمی‌گردونه (مثلا 62.413)، ولی تلگرام/دیتابیس int می‌خوان
        raw_duration = data.get('duration', None)
        duration = int(round(raw_duration)) if raw_duration is not None else None
        
        # توضیحات/کپشن کامل - yt-dlp این رو تو فیلد description برمی‌گردونه؛ فیلد
        # title معمولاً فقط یه خلاصه‌ی کوتاهه، پس بدون description کپشن کامل
        # هیچ‌وقت به کاربر نمی‌رسه (حتی اگه بعداً جایی truncate هم نشه)
        description = data.get('description', None)
        if description:
            description = description.strip() or None

        # ژانر
        genre = data.get('genre', None)
        
        # سال
        year = None
        if data.get('release_year'):
            year = data.get('release_year')
        elif data.get('upload_date'):
            year = int(data.get('upload_date', '')[:4])
        
        # تصویر
        thumbnail = None
        if data.get('thumbnail'):
            thumbnail = data.get('thumbnail')
        elif data.get('thumbnails'):
            thumbnails = data.get('thumbnails', [])
            if thumbnails:
                # بهترین کیفیت
                thumbnail = thumbnails[-1].get('url')
        
        return MediaMetadata(
            title=title,
            artist=artist,
            album=album,
            duration=duration,
            genre=genre,
            year=year,
            thumbnail_url=thumbnail,
            description=description,
            raw_data=data
        )
    
    @staticmethod
    async def extract_from_file(file_path: str) -> Optional[MediaMetadata]:
        """استخراج metadata از فایل محلی"""
        try:
            from mutagen.easyid3 import EasyID3
            from mutagen.flac import FLAC
            from mutagen.oggvorbis import OggVorbis
            from mutagen.oggopus import OggOpus
            
            path = Path(file_path)
            
            if not path.exists():
                return None
            
            # تلاش برای خواندن تگ‌ها
            try:
                # بیشتر MP3 فایل‌ها
                if path.suffix.lower() == '.mp3':
                    try:
                        audio = EasyID3(file_path)
                        return MetadataExtractor._parse_id3_tags(audio, path)
                    except Exception:
                        pass
                
                # FLAC
                if path.suffix.lower() == '.flac':
                    audio = FLAC(file_path)
                    return MetadataExtractor._parse_flac_tags(audio, path)
                
                # OGG
                if path.suffix.lower() == '.ogg':
                    try:
                        audio = OggOpus(file_path)
                    except Exception:
                        audio = OggVorbis(file_path)
                    return MetadataExtractor._parse_ogg_tags(audio, path)
            
            except Exception:
                pass
            
            # fallback: فقط نام فایل
            return MediaMetadata(
                title=path.stem,
                artist="هنرمند نامشخص"
            )
        
        except Exception as e:
            logger.warning(f"خطا در خواندن metadata فایل: {e}")
            return None
    
    @staticmethod
    def _parse_id3_tags(audio, path: Path) -> MediaMetadata:
        """تجزیه تگ‌های ID3 (شیء EasyID3 - تایپش فقط داخل extract_from_file ایمپورت می‌شه)"""
        return MediaMetadata(
            title=str(audio.get('title', [''])[0] or path.stem),
            artist=str(audio.get('artist', ['هنرمند نامشخص'])[0]),
            album=str(audio.get('album', [''])[0]),
            genre=str(audio.get('genre', [''])[0]),
        )

    @staticmethod
    def _parse_flac_tags(audio, path: Path) -> MediaMetadata:
        """تجزیه تگ‌های FLAC (شیء mutagen FLAC)"""
        return MediaMetadata(
            title=str(audio.get('title', [path.stem])[0]),
            artist=str(audio.get('artist', ['هنرمند نامشخص'])[0]),
            album=str(audio.get('album', [''])[0]),
            genre=str(audio.get('genre', [''])[0]),
            duration=int(audio.info.length) if audio.info.length else None,
        )

    @staticmethod
    def _parse_ogg_tags(audio, path: Path) -> MediaMetadata:
        """تجزیه تگ‌های OGG Vorbis (شیء mutagen OggVorbis/OggOpus)"""
        return MediaMetadata(
            title=str(audio.get('title', [path.stem])[0]),
            artist=str(audio.get('artist', ['هنرمند نامشخص'])[0]),
            album=str(audio.get('album', [''])[0]),
            genre=str(audio.get('genre', [''])[0]),
            duration=int(audio.info.length) if audio.info.length else None,
        )


def format_metadata_message(metadata: MediaMetadata) -> str:
    """فرمت کردن metadata برای نمایش"""
    msg = f"<b>🎵 {metadata.title}</b>\n"
    msg += f"👤 <b>هنرمند:</b> {metadata.artist}\n"
    
    if metadata.album:
        msg += f"💿 <b>آلبوم:</b> {metadata.album}\n"
    
    if metadata.duration_formatted:
        msg += f"⏱ <b>مدت:</b> {metadata.duration_formatted}\n"
    
    if metadata.genre:
        msg += f"🎼 <b>ژانر:</b> {metadata.genre}\n"
    
    if metadata.year:
        msg += f"📅 <b>سال:</b> {metadata.year}\n"
    
    return msg
