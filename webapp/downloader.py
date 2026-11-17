"""
رانر دانلود نسخه‌ی وب — بدون تلگرام، بدون Celery.

این ماژول عمداً از تسک Celery `worker.tasks.download_file` استفاده نمی‌کنه،
چون اون تسک به تلگرام چسبیده (پیام وضعیت، ارسال فایل به چت، آلبوم/ری‌اکشن).
به‌جاش همون شاخه‌های دانلود رو با استفاده‌ی مستقیم از ماژول‌های utils/
(که مستقل و قابل‌استفاده‌ی مجددن) دوباره پیاده می‌کنه:

- توییتر/ایکس: GraphQL مستقیم (fetch_tweet)
- پینترست: API داخلی (fetch_pinterest_pin) + HLS با ffmpeg
- اینستاگرامِ عکسی/کاروسل: API داخلی (fetch_instagram_post)
- فایل مستقیم/هاست ابری: resolverهای cloud_downloader + apk_downloader + MEGA
- اسپاتیفای: متادیتا از meta صفحه + صدا از جست‌وجوی یوتیوب، فال‌بک
  ساندکلاود و در آخر پیش‌نمایش رسمی ۳۰ ثانیه‌ای
- بقیه: yt-dlp با همون quality map و همون زنجیره‌ی retryهای worker/tasks.py
  (فال‌بک best، کوکی اینستاگرام، player_clientهای یوتیوب، tikwm برای تیک‌تاک)

نتیجه همیشه یه فایلِ روی دیسکِ که با webapp.storage زیر یه توکن ذخیره و از
/dl/ سرو می‌شه. وضعیت/پیشرفت در دیتابیس (DownloadJob) و یه store درون‌حافظه
نگه‌داری می‌شه تا فرانت‌اند با polling ببینه.
"""
import asyncio
import contextlib
import re
import shutil
import subprocess
import time
import zipfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from loguru import logger

from bot.config import settings
from db.crud import BotConfigCRUD, DownloadJobCRUD, DownloadStatsCRUD
from db.models import DownloadStatus, FileType
from db.session import async_session_maker
from utils.cache_key import get_cache_hash
from utils.error_classifier import classify_download_error
from utils.filename import sanitize_remote_filename
from utils.link_detector import SiteType
from utils.url_safety import url_is_safe_to_fetch
from webapp import storage

# ---------------------------------------------------------------------------
# کیفیت‌ها — دقیقاً همون map و لیبل‌های worker/tasks.py و bot/main.py
# ---------------------------------------------------------------------------

QUALITY_FORMAT_MAP = {
    "best": "bv*+ba/b",
    "1080p": "bv*[height<=1080]+ba/b[height<=1080]",
    "720p": "bv*[height<=720]+ba/b[height<=720]",
    "480p": "bv*[height<=480]+ba/b[height<=480]",
    # پیام ویدیویی به‌هرحال به 384x384 ریسایز می‌شه، پس کیفیت بالاتر لازم نیست
    "video_note": "bv*[height<=720]+ba/b[height<=720]",
}

QUALITY_LABELS = {
    "best": "🏆 بهترین کیفیت",
    "1080p": "1080p",
    "720p": "720p",
    "480p": "480p",
    "audio": "🎵 فقط صدا",
    "video_note": "⭕️ پیام ویدیویی (گرد)",
}

VALID_QUALITIES = ("best", "1080p", "720p", "480p", "audio", "video_note")

_IMPERSONATE_EXCLUDED_HOSTS = (
    "youtube.com", "youtu.be", "music.youtube.com", "instagram.com",
)
_YTDLP_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)%")
_STDBUF_PATH = shutil.which("stdbuf")
_MAX_PLAYLIST_TRACKS = 40

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _impersonate_args(url: str) -> list[str]:
    """--impersonate فقط برای سایت‌هایی که بهش نیاز دارن (مثل worker/tasks.py)."""
    if any(host in url for host in _IMPERSONATE_EXCLUDED_HOSTS):
        return []
    return ["--impersonate", "chrome"]


def _is_youtube_url(url: str) -> bool:
    return any(host in url for host in ("youtube.com", "youtu.be"))


# ---------------------------------------------------------------------------
# وضعیت پیشرفتِ درون‌حافظه + صف دانلودهای وب
# ---------------------------------------------------------------------------

_progress: dict[int, dict] = {}
_tasks: dict[int, asyncio.Task] = {}
_running: set[int] = set()
_waiting: deque[int] = deque()
_semaphore: Optional[asyncio.Semaphore] = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        limit = max(1, settings.WEB_MAX_CONCURRENT_DOWNLOADS)
        _semaphore = asyncio.Semaphore(limit)
    return _semaphore


def set_progress(job_id: int, stage: str, percent: Optional[float] = None, note: str = "") -> None:
    """وضعیت زنده‌ی یه جاب رو ثبت می‌کنه (فرانت‌اند با polling می‌خونه)."""
    _progress[job_id] = {
        "stage": stage,
        "percent": percent,
        "note": note,
        "updated_at": time.time(),
    }


def get_progress(job_id: int) -> dict:
    """وضعیت زنده‌ی یه جاب + جایگاهش تو صف (۰ یعنی در حال اجرا/تمام‌شده)."""
    entry = dict(_progress.get(job_id, {"stage": "", "percent": None, "note": ""}))
    if job_id in _waiting:
        entry["queue_position"] = list(_waiting).index(job_id) + 1
    else:
        entry["queue_position"] = 0
    entry["active_count"] = len(_running) + len(_waiting)
    return entry


@dataclass
class JobSnapshot:
    """فیلدهای لازم از DownloadJob، جدا از سشن دیتابیس (برای دانلود طولانی)."""
    id: int
    user_id: int
    url: str
    detected_site: str
    file_type: FileType
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    duration: Optional[int] = None
    thumbnail_url: Optional[str] = None
    description: Optional[str] = None
    selected_quality: Optional[str] = None
    direct_link_filename: Optional[str] = None


async def _load_job(job_id: int) -> Optional[JobSnapshot]:
    async with async_session_maker() as session:
        job = await DownloadJobCRUD.get_by_id(session, job_id)
        if not job:
            return None
        return JobSnapshot(
            id=job.id, user_id=job.user_id, url=job.url,
            detected_site=job.detected_site, file_type=job.file_type,
            title=job.title, artist=job.artist, album=job.album,
            duration=job.duration, thumbnail_url=job.thumbnail_url,
            description=job.description, selected_quality=job.selected_quality,
            direct_link_filename=job.direct_link_filename,
        )


async def _update(job_id: int, status: DownloadStatus, **kwargs) -> None:
    async with async_session_maker() as session:
        await DownloadJobCRUD.update_status(session, job_id, status, **kwargs)


async def _get_max_file_size_mb() -> int:
    """سقف حجم فایل از BotConfig (پنل /maxsize) با فال‌بک به تنظیمات env."""
    try:
        async with async_session_maker() as session:
            return await BotConfigCRUD.get_int(
                session, "max_file_size_mb", settings.MAX_FILE_SIZE_MB
            )
    except Exception:
        return settings.MAX_FILE_SIZE_MB


async def _increment_stats(successful: bool, size_mb: float, site: str) -> None:
    try:
        async with async_session_maker() as session:
            await DownloadStatsCRUD.increment_today(
                session, successful=successful, size_mb=size_mb, site=site
            )
    except Exception as e:
        logger.warning(f"ثبت آمار وب ناموفق بود: {e}")


# ---------------------------------------------------------------------------
# ابزارهای پایه: HEAD probe، دانلود generic، HLS، yt-dlp
# ---------------------------------------------------------------------------

async def probe_remote_size_mb(url: str) -> Optional[float]:
    """
    حجم فایل رو با HEAD سبک می‌پرسه (بدون دانلود). ریدایرکت‌ها دستی دنبال
    می‌شن و مقصد هر hop دوباره SSRF-check می‌شه (مثل bot/main.py).
    اگه چیزی معلوم نشد None برمی‌گردونه (یعنی: هشدار نده).
    """
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=8.0) as client:
            current_url = url
            for _hop in range(5):
                if not await url_is_safe_to_fetch(current_url):
                    return None
                resp = await client.head(current_url, headers={"User-Agent": _USER_AGENT})
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        return None
                    current_url = str(resp.url.join(location))
                    continue
                size = resp.headers.get("content-length")
                if size and size.isdigit():
                    return int(size) / (1024 * 1024)
                return None
    except Exception as e:
        logger.debug(f"HEAD probe وب برای حجم فایل ناموفق بود: {e}")
    return None


async def _download_generic_file(
    url: str, temp_dir: Path, job_id: int, max_size_mb: int,
    extra_headers: Optional[dict] = None,
) -> Path:
    """
    دانلود استریمی فایل مستقیم با httpx (الگو: worker/tasks.py).
    ریدایرکت‌ها دستی دنبال و هر hop SSRF-check می‌شه؛ اگه حجم از سقف رد بشه
    وسط دانلود متوقف می‌شه.
    """
    headers = {"User-Agent": _USER_AGENT, "Accept": "*/*"}
    if extra_headers:
        headers.update(extra_headers)
    max_bytes = max_size_mb * 1024 * 1024

    async with httpx.AsyncClient(follow_redirects=False, timeout=60.0, headers=headers) as client:
        current_url = url
        for _hop in range(10):
            if not await url_is_safe_to_fetch(current_url):
                raise RuntimeError("لینک به یه آدرس داخلی/خصوصی سرور اشاره می‌کنه و دانلودش رد شد.")
            async with client.stream("GET", current_url) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise RuntimeError("سرور ریدایرکت بدون مقصد برگردوند.")
                    current_url = str(response.url.join(location))
                    continue
                response.raise_for_status()

                filename = None
                content_disposition = response.headers.get("content-disposition", "")
                match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', content_disposition)
                if match:
                    filename = match.group(1).strip()
                if not filename:
                    final_path = str(response.url).split("?")[0]
                    filename = final_path.rsplit("/", 1)[-1] or "downloaded_file"
                filename = sanitize_remote_filename(filename) or "downloaded_file"

                file_path = temp_dir / filename
                total = int(response.headers.get("content-length") or 0)
                if total and total > max_bytes:
                    raise RuntimeError(
                        f"حجم فایل ({total / (1024 * 1024):.0f} مگابایت) از سقف مجاز "
                        f"({max_size_mb} مگابایت) بیشتر است."
                    )
                downloaded = 0
                with open(file_path, "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            raise RuntimeError(
                                f"حجم فایل از سقف مجاز ({max_size_mb} مگابایت) بیشتر شد."
                            )
                        if total:
                            set_progress(job_id, "downloading", downloaded / total * 100)
                        else:
                            set_progress(
                                job_id, "downloading", None,
                                f"{downloaded / (1024 * 1024):.1f} مگابایت دانلود شد...",
                            )
                return file_path
    raise RuntimeError("لینک بین بیش از ۱۰ ریدایرکت حرکت کرد (حلقه‌ی ریدایرکت).")


async def _download_hls_with_ffmpeg(hls_url: str, temp_dir: Path, filename: str = "video.mp4") -> Optional[Path]:
    """استریم HLS (.m3u8) رو با ffmpeg به mp4 تبدیل می‌کنه (مثل worker/tasks.py)."""
    if not await url_is_safe_to_fetch(hls_url):
        raise RuntimeError("لینک HLS به یه آدرس داخلی اشاره می‌کنه و رد شد.")
    output_path = temp_dir / filename

    def _run():
        return subprocess.run(
            ["ffmpeg", "-y", "-i", hls_url, "-c", "copy", "-bsf:a", "aac_adtstoasc", str(output_path)],
            capture_output=True, timeout=180,
        )

    try:
        result = await asyncio.to_thread(_run)
    except Exception as e:
        logger.error(f"خطا در دانلود HLS با ffmpeg: {e}")
        return None
    if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
        return output_path
    logger.error(f"دانلود HLS با ffmpeg ناموفق بود: {result.stderr.decode(errors='ignore')[:500]}")
    return None


async def _run_ytdlp(cmd: list[str], job_id: int, timeout: int) -> tuple[int, str]:
    """
    اجرای async yt-dlp با پارس درصد پیشرفت از stdout (الگو:
    worker/tasks.py::_run_ytdlp_with_progress، بدون پیام تلگرام).
    برمی‌گردونه: (returncode, stderr).
    """
    full_cmd = [*cmd, "--newline"]
    if _STDBUF_PATH:
        full_cmd = [_STDBUF_PATH, "-oL", "-eL", *full_cmd]
    env = {**__import__("os").environ, "PYTHONUNBUFFERED": "1"}
    proc = await asyncio.create_subprocess_exec(
        *full_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stderr_chunks: list[bytes] = []

    async def _read_stdout():
        async for raw_line in proc.stdout:
            line = raw_line.decode(errors="ignore").strip()
            m = _YTDLP_PERCENT_RE.search(line)
            if "[download]" in line and m:
                try:
                    set_progress(job_id, "downloading", float(m.group(1)))
                except ValueError:
                    pass
            elif "[Merger]" in line or "Merging formats" in line:
                set_progress(job_id, "merging", 100, "در حال ادغام ویدیو و صدا...")
            elif "[ExtractAudio]" in line:
                set_progress(job_id, "converting", 100, "در حال تبدیل به MP3...")

    async def _read_stderr():
        async for raw_line in proc.stderr:
            stderr_chunks.append(raw_line)

    try:
        await asyncio.wait_for(
            asyncio.gather(_read_stdout(), _read_stderr(), proc.wait()), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        stderr_text = b"".join(stderr_chunks).decode(errors="ignore")
        return 1, stderr_text + "\nخطا: دانلود بیش از حد مجاز طول کشید (timeout)"
    return proc.returncode, b"".join(stderr_chunks).decode(errors="ignore")


async def _probe_soundcloud_playlist_tracks(url: str) -> Optional[list[str]]:
    """مثل worker/tasks.py: با --flat-playlist می‌پرسه ساندکلاود پلی‌لیسته یا تک‌ترک."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp", "--flat-playlist", "--print", "%(webpage_url)s", url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=45)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return None
        if proc.returncode != 0:
            return None
        track_urls = [ln.strip() for ln in stdout_bytes.decode(errors="ignore").splitlines() if ln.strip()]
        return track_urls or None
    except Exception as e:
        logger.debug(f"probe پلی‌لیست ساندکلاود (وب) ناموفق بود: {e}")
        return None


def _pick_downloaded_file(temp_dir: Path) -> Optional[Path]:
    """بزرگ‌ترین فایل واقعیِ دانلودشده تو پوشه‌ی موقت (بدون فایل‌های جانبی)."""
    ignored_suffixes = {".part", ".ytdl", ".temp", ".tmp", ".json", ".description"}
    candidates = []
    for path in temp_dir.iterdir():
        if not path.is_file() or path.suffix.lower() in ignored_suffixes:
            continue
        if path.name.startswith("."):
            continue
        candidates.append(path)
    if not candidates:
        return None
    # اگه فقط تامبنیل مونده (jpg/webp) و هیچ مدیایی نیست، همون رو برگردون
    # (caller بر اساس file_type تصمیم می‌گیره)؛ وگرنه بزرگ‌ترین فایل.
    media = [p for p in candidates if p.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp")]
    pool = media if media else candidates
    return max(pool, key=lambda p: p.stat().st_size)


async def _prepare_video_note(video_path: Path, temp_dir: Path) -> Optional[Path]:
    """تبدیل به ویدیوی مربعی 384x384 حداکثر ۶۰ ثانیه (مثل worker/tasks.py)."""
    output_path = temp_dir / "video_note.mp4"

    def _run():
        return subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(video_path), "-t", "60",
                "-vf", "crop='min(iw,ih)':'min(iw,ih)',scale=384:384",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                str(output_path),
            ],
            capture_output=True, timeout=180,
        )

    try:
        result = await asyncio.to_thread(_run)
    except Exception as e:
        logger.warning(f"ساخت پیام ویدیویی (وب) ناموفق بود: {e}")
        return None
    if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
        return output_path
    return None


def _zip_files(files: list[tuple[str, Path]], dest: Path) -> Path:
    """چند فایل رو تو یه ZIP می‌ذاره (کاروسل اینستاگرام، پوشه‌ی Gofile)."""
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_STORED) as zf:
        for arcname, path in files:
            zf.write(path, arcname=arcname)
    return dest


# ---------------------------------------------------------------------------
# شاخه‌های دانلود (هر کدوم یه Path برمی‌گردونه یا Exception می‌ده)
# ---------------------------------------------------------------------------

async def _download_twitter(job: JobSnapshot, temp_dir: Path, job_id: int, max_size_mb: int) -> Path:
    from utils.twitter_downloader import fetch_tweet

    tweet = await fetch_tweet(job.url)
    if not tweet or not tweet.medias:
        raise RuntimeError("مدیایی برای این توییت پیدا نشد")
    wanted_type = "video" if job.file_type == FileType.VIDEO else "photo"
    media = next((m for m in tweet.medias if m.type == wanted_type), tweet.medias[0])
    file_path = await _download_generic_file(media.url, temp_dir, job_id, max_size_mb)
    if not file_path.suffix:
        ext = ".mp4" if media.type == "video" else ".jpg"
        new_path = file_path.with_suffix(ext)
        file_path.rename(new_path)
        file_path = new_path
    return file_path


async def _download_pinterest(job: JobSnapshot, temp_dir: Path, job_id: int, max_size_mb: int) -> Path:
    from utils.pinterest_downloader import fetch_pinterest_pin

    pin_media = await fetch_pinterest_pin(job.url)
    if not pin_media:
        raise RuntimeError("مدیایی برای این پین پیدا نشد")
    if pin_media.is_hls:
        set_progress(job_id, "downloading", None, "در حال دانلود ویدیوی Story Pin...")
        file_path = await _download_hls_with_ffmpeg(pin_media.url, temp_dir)
        if not file_path:
            raise RuntimeError("دانلود ویدیوی HLS پینترست ناموفق بود")
    else:
        file_path = await _download_generic_file(pin_media.url, temp_dir, job_id, max_size_mb)
    if not file_path.suffix:
        ext = ".mp4" if pin_media.type == "video" else ".jpg"
        new_path = file_path.with_suffix(ext)
        file_path.rename(new_path)
        file_path = new_path
    return file_path


async def _download_instagram_photo(job: JobSnapshot, temp_dir: Path, job_id: int, max_size_mb: int) -> Path:
    """
    پست عکسی/کاروسل اینستاگرام (yt-dlp پشتیبانی نمی‌کنه). کاروسل یا پستِ
    همراه با موزیک به‌صورت ZIP تحویل داده می‌شه (معادل وبِ «آلبوم» بات).
    """
    from utils.instagram_downloader import fetch_instagram_post

    ig_post = await fetch_instagram_post(job.url, cookie_file=settings.INSTAGRAM_COOKIE_FILE)
    if not ig_post or not ig_post.items:
        raise RuntimeError("مدیایی برای این پست اینستاگرام پیدا نشد")
    downloaded: list[tuple[str, Path]] = []
    for idx, item in enumerate(ig_post.items[:10]):
        try:
            sub_path = await _download_generic_file(item.url, temp_dir, job_id, max_size_mb)
            if not sub_path.suffix:
                ext = ".mp4" if item.type == "video" else ".jpg"
                renamed = sub_path.with_name(f"{sub_path.stem}_{idx}{ext}")
                sub_path.rename(renamed)
                sub_path = renamed
            downloaded.append((f"{idx + 1}_{sub_path.name}", sub_path))
        except Exception as e:
            logger.warning(f"دانلود آیتم {idx} کاروسل (وب) ناموفق بود: {e}")
    if not downloaded:
        raise RuntimeError("هیچ‌کدوم از آیتم‌های پست دانلود نشدن")
    if ig_post.music_url:
        try:
            music_path = await _download_generic_file(ig_post.music_url, temp_dir, job_id, max_size_mb)
            downloaded.append((f"music_{music_path.name}", music_path))
        except Exception as e:
            logger.warning(f"دانلود موزیک پس‌زمینه‌ی اینستاگرام (وب) ناموفق بود: {e}")
    if len(downloaded) == 1:
        return downloaded[0][1]
    zip_path = temp_dir / "instagram_post.zip"
    return _zip_files(downloaded, zip_path)


async def _download_document(job: JobSnapshot, temp_dir: Path, job_id: int, max_size_mb: int) -> Path:
    """لینک‌های مستقیم فایل / هاست ابری / APKPure / MEGA (مثل worker/tasks.py)."""
    from utils.apk_downloader import resolve_apkpure_download
    from utils.cloud_downloader import (
        download_mega_file,
        resolve_dropbox,
        resolve_filelu,
        resolve_gofile,
        resolve_google_drive,
        resolve_mediafire,
        resolve_pixeldrain,
    )

    site = job.detected_site
    download_url: Optional[str] = job.url
    extra_headers: Optional[dict] = None
    file_path: Optional[Path] = None

    if site == SiteType.APKPURE.value:
        apk_info = await resolve_apkpure_download(job.url)
        if not apk_info:
            raise RuntimeError("لینک مستقیم دانلود پیدا نشد (احتمالاً ساختار صفحه‌ی APKPure عوض شده)")
        download_url = apk_info.download_url
    elif site == SiteType.PIXELDRAIN.value:
        pd_info = await resolve_pixeldrain(job.url)
        if not pd_info:
            raise RuntimeError("فایل روی Pixeldrain پیدا نشد (احتمالاً حذف شده یا لینک اشتباهه)")
        download_url = pd_info.download_url
    elif site == SiteType.DROPBOX.value:
        db_info = resolve_dropbox(job.url)
        if not db_info:
            raise RuntimeError("لینک Dropbox قابل تبدیل به لینک مستقیم نبود")
        download_url = db_info.download_url
    elif site == SiteType.GOFILE.value:
        gf_files = await resolve_gofile(job.url)
        if not gf_files:
            raise RuntimeError("محتوای این لینک Gofile پیدا نشد (ممکنه پوشه پسورد داشته باشه)")
        if len(gf_files) == 1:
            download_url = gf_files[0].download_url
            extra_headers = gf_files[0].extra_headers
        else:
            # پوشه‌ی چندفایلی: همه رو دانلود و ZIP می‌کنیم (بات فقط اولی رو می‌گرفت)
            parts: list[tuple[str, Path]] = []
            for gf in gf_files:
                part = await _download_generic_file(
                    gf.download_url, temp_dir, job_id, max_size_mb, extra_headers=gf.extra_headers
                )
                parts.append((sanitize_remote_filename(gf.title) or part.name, part))
            zip_path = temp_dir / "gofile_files.zip"
            return _zip_files(parts, zip_path)
    elif site == SiteType.FILELU.value:
        fl_info = await resolve_filelu(job.url, settings.FILELU_API_KEY)
        if not fl_info:
            raise RuntimeError("دانلود از FileLu ناموفق بود (کلید API تنظیم نشده یا فایل حذف شده)")
        download_url = fl_info.download_url
    elif site == SiteType.MEDIAFIRE.value:
        mf_info = await resolve_mediafire(job.url)
        if not mf_info:
            raise RuntimeError("لینک مستقیم دانلود از MediaFire پیدا نشد")
        download_url = mf_info.download_url
    elif site == SiteType.GOOGLE_DRIVE.value:
        gd_info = await resolve_google_drive(job.url)
        if not gd_info:
            raise RuntimeError("لینک Google Drive قابل پردازش نبود (شاید private باشه)")
        download_url = gd_info.download_url
    elif site == SiteType.MEGA.value:
        set_progress(job_id, "downloading", None, "در حال دانلود و رمزگشایی از MEGA...")
        mega_info = await download_mega_file(job.url, str(temp_dir))
        if not mega_info:
            raise RuntimeError("دانلود از MEGA ناموفق بود (لینک نامعتبره یا mega.py نصب نیست)")
        file_path = Path(mega_info.download_url)
        download_url = None

    if download_url is not None:
        file_path = await _download_generic_file(
            download_url, temp_dir, job_id, max_size_mb, extra_headers=extra_headers
        )
    if not file_path or not file_path.exists():
        raise RuntimeError("فایلی پیدا نشد")
    return file_path


async def _youtube_403_retries(
    cmd: list[str], job_id: int, stderr_text: str
) -> tuple[int, str]:
    """زنجیره‌ی player_client جایگزین بعد از 403 یوتیوب (مثل worker/tasks.py)."""
    returncode, stderr = 1, stderr_text
    for client in ("mweb", "android", "tv"):
        logger.warning(f"job {job_id} (وب): یوتیوب 403 داد، تلاش با player_client={client}")
        set_progress(job_id, "retrying", 0, "یوتیوب موقتاً درخواست رو رد کرد، تلاش دوباره...")
        retry_args = ["--extractor-args", f"youtube:player_client={client}"]
        if settings.YOUTUBE_COOKIE_FILE:
            retry_args += ["--cookies", settings.YOUTUBE_COOKIE_FILE]
        retry_cmd = cmd[:-1] + retry_args + [cmd[-1]]
        returncode, stderr = await _run_ytdlp(retry_cmd, job_id, settings.DOWNLOAD_TIMEOUT)
        if returncode == 0:
            break
    return returncode, stderr


async def _download_spotify_track(
    job: JobSnapshot, temp_dir: Path, job_id: int, max_size_mb: int
) -> tuple[Path, Optional[str]]:
    """
    تک‌آهنگ اسپاتیفای: زنجیره‌ی یوتیوب → ساندکلاود → پیش‌نمایش ۳۰ ثانیه‌ای.
    برمی‌گردونه: (مسیر فایل، پسوند عنوان اگه پیش‌نمایش استفاده شده).
    """
    from utils.spotify_downloader import (
        build_search_query,
        fetch_spotify_track,
        tag_audio_file,
    )

    track = await fetch_spotify_track(job.url)
    if not track:
        raise RuntimeError("اطلاعات این آهنگ از اسپاتیفای گرفته نشد")
    search_query = build_search_query(track)
    outtmpl = str(temp_dir / "%(title)s.%(ext)s")
    cmd = [
        "yt-dlp", f"ytsearch1:{search_query} audio",
        "-x", "--audio-format", "mp3", "--audio-quality", "0",
        "-o", outtmpl, "--no-playlist",
    ]
    returncode, stderr_text = await _run_ytdlp(cmd, job_id, settings.DOWNLOAD_TIMEOUT)
    if returncode != 0 and "403" in (stderr_text or ""):
        returncode, stderr_text = await _youtube_403_retries(cmd, job_id, stderr_text)
    if returncode != 0:
        logger.warning(f"job {job_id} (وب): جست‌وجوی یوتیوب ناموفق بود، تلاش با ساندکلاود...")
        set_progress(job_id, "retrying", 0, "یوتیوب جواب نداد، امتحان با ساندکلاود...")
        sc_cmd = [cmd[0], f"scsearch1:{search_query} audio"] + cmd[2:]
        returncode, stderr_text = await _run_ytdlp(sc_cmd, job_id, settings.DOWNLOAD_TIMEOUT)

    preview_suffix = None
    if returncode != 0 and track.preview_url:
        logger.warning(f"job {job_id} (وب): استفاده از پیش‌نمایش رسمی اسپاتیفای...")
        set_progress(job_id, "retrying", 0, "فایل کامل در دسترس نبود؛ پیش‌نمایش رسمی می‌گیرم...")
        try:
            prev_path = await _download_generic_file(track.preview_url, temp_dir, job_id, max_size_mb)
            preview_suffix = " (پیش‌نمایش ۳۰ ثانیه‌ای)"
            return prev_path, preview_suffix
        except Exception as e:
            logger.warning(f"دانلود پیش‌نمایش اسپاتیفای (وب) ناموفق بود: {e}")
    if returncode != 0:
        raise RuntimeError(classify_download_error(stderr_text) or "جست‌وجوی آهنگ نتیجه‌ای نداد")

    file_path = _pick_downloaded_file(temp_dir)
    if not file_path:
        raise RuntimeError("فایل صوتی پیدا نشد")
    # تگ ID3 با متادیتای واقعی اسپاتیفای (نه عنوان خام ویدیوی یوتیوب)
    if file_path.suffix.lower() == ".mp3":
        try:
            await tag_audio_file(file_path, track)
        except Exception as e:
            logger.warning(f"تگ‌گذاری فایل اسپاتیفای (وب) ناموفق بود: {e}")
    # عنوان/هنرمند/آلبوم واقعی رو روی جاب بنویس
    await _update(
        job_id, DownloadStatus.DOWNLOADING,
        title=track.title, artist=track.artist, album=track.album,
        thumbnail_url=track.cover_url,
    )
    return file_path, preview_suffix


async def _expand_playlist(
    job: JobSnapshot, track_urls: list[str], site_value: str, file_type: FileType
) -> int:
    """
    فن‌اوت پلی‌لیست/آلبوم به jobهای فرزند با playlist_id مشترک (خودِ id والد).
    هر فرزند جدا صف‌بندی و دانلود می‌شه؛ والد DONE می‌شه و فرانت‌اند فرزندها
    رو از روی playlist_id نشون می‌ده.
    """
    track_urls = track_urls[:_MAX_PLAYLIST_TRACKS]
    playlist_batch_id = str(job.id)
    spawned = 0
    for idx, track_url in enumerate(track_urls, start=1):
        try:
            async with async_session_maker() as session:
                sub_job = await DownloadJobCRUD.create(
                    session,
                    user_id=job.user_id,
                    chat_id=0,
                    url=track_url,
                    detected_site=site_value,
                    raw_domain=urlparse(track_url).netloc.lower(),
                    file_type=file_type,
                    playlist_id=playlist_batch_id,
                    playlist_index=idx,
                    playlist_total=len(track_urls),
                    selected_quality="best",
                )
            enqueue_download(sub_job.id, "best")
            spawned += 1
        except Exception as e:
            logger.warning(f"ساخت job فرزند پلی‌لیست (وب) برای {track_url} ناموفق بود: {e}")
    await _update(
        job.id, DownloadStatus.DONE,
        playlist_id=playlist_batch_id, playlist_total=len(track_urls),
    )
    set_progress(job.id, "playlist", 100, f"{spawned} آهنگ پیدا شد و تو صف دانلود رفت.")
    return spawned


async def _download_ytdlp_generic(
    job: JobSnapshot, temp_dir: Path, job_id: int, quality: str, max_size_mb: int
) -> Path:
    """مسیر عمومی yt-dlp (یوتیوب، اینستاگرامِ ویدیویی، تیک‌تاک، آپارات و ...)."""
    outtmpl = str(temp_dir / "%(title)s.%(ext)s")
    is_instagram_job = job.detected_site in (SiteType.INSTAGRAM.value, SiteType.INSTAGRAM_REEL.value)
    instagram_cookie_args = (
        ["--cookies", settings.INSTAGRAM_COOKIE_FILE]
        if is_instagram_job and settings.INSTAGRAM_COOKIE_FILE
        else []
    )
    video_format = None
    if job.file_type == FileType.AUDIO or quality == "audio":
        cmd = [
            "yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", "0",
            "--embed-thumbnail", "--convert-thumbnail", "jpg",
            "-o", outtmpl, "--no-playlist",
            *_impersonate_args(job.url), job.url,
        ]
    else:
        video_format = QUALITY_FORMAT_MAP.get(quality, "bv*+ba/b")
        cmd = [
            "yt-dlp", "--format", video_format,
            "-S", "vcodec:h264,acodec:aac",
            "--merge-output-format", "mp4",
            "-o", outtmpl, "--no-playlist",
            *_impersonate_args(job.url), job.url,
        ]

    returncode, stderr_text = await _run_ytdlp(cmd, job_id, settings.DOWNLOAD_TIMEOUT)

    # فال‌بک best وقتی فرمت انتخابی در دسترس نیست
    if (
        returncode != 0
        and "Requested format is not available" in (stderr_text or "")
        and video_format is not None
        and video_format != "bv*+ba/b"
    ):
        logger.warning(f"job {job_id} (وب): فرمت {quality} در دسترس نبود، تلاش با best")
        set_progress(job_id, "retrying", 0, "کیفیت انتخابی در دسترس نبود، تلاش با بهترین کیفیت...")
        fallback_cmd = [
            "yt-dlp", "--format", "bv*+ba/b",
            "-S", "vcodec:h264,acodec:aac",
            "--merge-output-format", "mp4",
            "-o", outtmpl, "--no-playlist",
            *_impersonate_args(job.url), job.url,
        ]
        returncode, stderr_text = await _run_ytdlp(fallback_cmd, job_id, settings.DOWNLOAD_TIMEOUT)

    # تلاش دوم با کوکی اینستاگرام (فقط وقتی علامت لاگین/ریت‌لیمیت هست)
    if (
        returncode != 0
        and is_instagram_job
        and instagram_cookie_args
        and any(
            marker in (stderr_text or "")
            for marker in (
                "login_required", "login required", "rate-limit",
                "HTTP Error 400", "HTTP Error 401", "HTTP Error 404",
            )
        )
    ):
        logger.warning(f"job {job_id} (وب): yt-dlp بدون کوکی fail شد، تلاش با کوکی اینستاگرام")
        retry_cmd = cmd[:-1] + [*instagram_cookie_args, cmd[-1]]
        returncode, stderr_text = await _run_ytdlp(retry_cmd, job_id, settings.DOWNLOAD_TIMEOUT)

    # زنجیره‌ی player_client یوتیوب بعد از 403
    if returncode != 0 and _is_youtube_url(job.url) and "403" in (stderr_text or ""):
        returncode, stderr_text = await _youtube_403_retries(cmd, job_id, stderr_text)

    # فال‌بک تیک‌تاک از طریق tikwm.com وقتی اکسترکتور وب yt-dlp fail می‌شه
    if returncode != 0 and job.detected_site == SiteType.TIKTOK.value:
        try:
            from utils.tiktok_downloader import fetch_tiktok_video

            logger.warning(f"job {job_id} (وب): yt-dlp تیک‌تاک fail شد، تلاش با tikwm.com...")
            set_progress(job_id, "retrying", 0, "تلاش با مسیر جایگزین تیک‌تاک...")
            media = await fetch_tiktok_video(job.url)
            if media and not media.is_image_post:
                media_url = media.audio_url if (job.file_type == FileType.AUDIO or quality == "audio") else media.video_url
                media_url = media_url or media.video_url or media.audio_url
                if media_url:
                    file_path = await _download_generic_file(media_url, temp_dir, job_id, max_size_mb)
                    if media.title:
                        await _update(job_id, DownloadStatus.DOWNLOADING, title=media.title)
                    return file_path
        except Exception as e:
            logger.warning(f"فال‌بک tikwm (وب) ناموفق بود: {e}")

    if returncode != 0:
        raise RuntimeError(classify_download_error(stderr_text) or "yt-dlp نتوانست این لینک را دانلود کند.")
    file_path = _pick_downloaded_file(temp_dir)
    if not file_path:
        raise RuntimeError("فایلی بعد از دانلود پیدا نشد")
    return file_path


# ---------------------------------------------------------------------------
# اجرای اصلی + صف‌بندی
# ---------------------------------------------------------------------------

async def _execute(job_id: int, quality: str, custom_filename: Optional[str]) -> None:
    job = await _load_job(job_id)
    if not job:
        return
    temp_dir = Path(settings.TEMP_DIR) / f"web_{job_id}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    cancelled = False
    try:
        _waiting.append(job_id)
        set_progress(job_id, "queued", 0, "در صف دانلود...")
        await _get_semaphore().acquire()
        _running.add(job_id)
        try:
            # اگه موقع انتظار تو صف لغو شده بود، شروع نکن
            fresh = await _load_job(job_id)
            if fresh and fresh.selected_quality:
                quality = fresh.selected_quality
            async with async_session_maker() as session:
                current = await DownloadJobCRUD.get_by_id(session, job_id)
                if current and current.status == DownloadStatus.CANCELLED:
                    return
            await _update(job_id, DownloadStatus.DOWNLOADING)
            set_progress(job_id, "downloading", 0, "در حال دانلود...")
            max_size_mb = await _get_max_file_size_mb()

            file_path: Optional[Path] = None
            preview_suffix: Optional[str] = None

            if job.detected_site == SiteType.TWITTER.value:
                file_path = await _download_twitter(job, temp_dir, job_id, max_size_mb)
            elif job.detected_site == SiteType.PINTEREST.value:
                file_path = await _download_pinterest(job, temp_dir, job_id, max_size_mb)
            elif (
                job.detected_site in (SiteType.INSTAGRAM.value, SiteType.INSTAGRAM_REEL.value)
                and job.file_type == FileType.PHOTO
            ):
                file_path = await _download_instagram_photo(job, temp_dir, job_id, max_size_mb)
            elif job.file_type == FileType.DOCUMENT:
                file_path = await _download_document(job, temp_dir, job_id, max_size_mb)
            elif job.detected_site == SiteType.SPOTIFY.value:
                from utils.spotify_downloader import (
                    fetch_spotify_playlist_tracks,
                    is_playlist_or_album,
                )

                if is_playlist_or_album(job.url):
                    track_urls = await fetch_spotify_playlist_tracks(job.url)
                    if not track_urls:
                        raise RuntimeError("نتونستم لیست ترک‌های این پلی‌لیست/آلبوم رو از اسپاتیفای بگیرم.")
                    spawned = await _expand_playlist(job, track_urls, SiteType.SPOTIFY.value, FileType.AUDIO)
                    if spawned == 0:
                        raise RuntimeError("هیچ‌کدوم از ترک‌های این پلی‌لیست قابل صف‌کردن نبودن.")
                    await _increment_stats(True, 0, job.detected_site)
                    return
                file_path, preview_suffix = await _download_spotify_track(job, temp_dir, job_id, max_size_mb)
            elif job.detected_site == SiteType.SOUNDCLOUD.value:
                playlist_track_urls = await _probe_soundcloud_playlist_tracks(job.url)
                if playlist_track_urls and len(playlist_track_urls) > 1:
                    spawned = await _expand_playlist(
                        job, playlist_track_urls, SiteType.SOUNDCLOUD.value, FileType.AUDIO
                    )
                    if spawned == 0:
                        raise RuntimeError("هیچ‌کدوم از ترک‌های این پلی‌لیست قابل صف‌کردن نبودن.")
                    await _increment_stats(True, 0, job.detected_site)
                    return
                file_path = await _download_ytdlp_generic(job, temp_dir, job_id, quality, max_size_mb)
            else:
                file_path = await _download_ytdlp_generic(job, temp_dir, job_id, quality, max_size_mb)

            if not file_path or not file_path.exists():
                raise RuntimeError("فایلی پیدا نشد")

            # پیام ویدیویی: تبدیل به مربعی 384x384 (اگه ffmpeg fail شد، خودِ فایل اصلی می‌مونه)
            if quality == "video_note" and job.file_type != FileType.AUDIO:
                set_progress(job_id, "converting", 100, "در حال آماده‌سازی پیام ویدیویی...")
                converted = await _prepare_video_note(file_path, temp_dir)
                if converted:
                    file_path = converted

            size_mb = file_path.stat().st_size / (1024 * 1024)
            if size_mb > max_size_mb:
                raise RuntimeError(
                    f"حجم فایل ({size_mb:.0f} مگابایت) از سقف مجاز ({max_size_mb} مگابایت) بیشتر است."
                )

            # ذخیره زیر توکن + ثبت لینک مستقیم + کش وب
            set_progress(job_id, "finishing", 100, "در حال آماده‌سازی لینک دانلود...")
            token, filename, dest_path = storage.store_file(job_id, file_path, custom_filename)
            from datetime import datetime, timedelta

            expires_at = datetime.utcnow() + timedelta(seconds=settings.DIRECT_LINK_EXPIRE_SECONDS)
            download_url = storage.build_download_url(token, filename)
            async with async_session_maker() as session:
                await DownloadJobCRUD.set_direct_link(session, job_id, token, download_url, expires_at)

            title = job.title
            if preview_suffix and title:
                title = f"{title}{preview_suffix}"
            await _update(
                job_id, DownloadStatus.DONE,
                file_name=filename, file_size_mb=round(size_mb, 2),
                title=title, error_msg=None,
            )
            try:
                cache_hash = get_cache_hash(job.url, quality, job.file_type.value)
                storage.put_cache(
                    cache_hash, dest_path,
                    {"title": title, "artist": job.artist, "album": job.album,
                     "duration": job.duration, "file_type": job.file_type.value},
                )
            except Exception as e:
                logger.warning(f"ذخیره‌ی کش وب برای job {job_id} ناموفق بود: {e}")
            await _increment_stats(True, size_mb, job.detected_site)
            set_progress(job_id, "done", 100, "دانلود کامل شد.")
        finally:
            _get_semaphore().release()
            _running.discard(job_id)
    except asyncio.CancelledError:
        cancelled = True
        raise
    except Exception as e:
        # پیام فارسیِ طبقه‌بندی‌شده اگه الگویی شناخته شد، وگرنه خودِ پیام
        # خطا (که تو شاخه‌های دانلود از قبل فارسی و مشخص نوشته شده)
        reason = classify_download_error(e) or classify_download_error(str(e)) or str(e) or "دانلود با خطا مواجه شد."
        logger.error(f"دانلود وب job {job_id} ناموفق بود: {e}")
        await _update(job_id, DownloadStatus.FAILED, error_msg=reason[:1000])
        await _increment_stats(False, 0, job.detected_site)
        set_progress(job_id, "failed", None, reason)
    finally:
        if job_id in _waiting:
            _waiting.remove(job_id)
        if cancelled:
            await _update(job_id, DownloadStatus.CANCELLED, error_msg="دانلود توسط کاربر لغو شد.")
            set_progress(job_id, "cancelled", None, "لغو شد.")
        _tasks.pop(job_id, None)
        try:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
        except Exception as e:
            logger.warning(f"پاک‌سازی پوشه‌ی موقت وب job {job_id} ناموفق بود: {e}")


def enqueue_download(job_id: int, quality: str = "best", custom_filename: Optional[str] = None) -> asyncio.Task:
    """یه جاب وب رو تو صف دانلود می‌ذاره (تسک asyncio درون پروسه‌ی وب)."""
    existing = _tasks.get(job_id)
    if existing and not existing.done():
        return existing
    set_progress(job_id, "queued", 0, "در صف دانلود...")
    task = asyncio.create_task(_execute(job_id, quality, custom_filename))
    _tasks[job_id] = task
    return task


async def cancel_download(job_id: int) -> bool:
    """لغو یه دانلود وبِ در حال اجرا/در صف. False اگه تسک فعالی نباشه."""
    task = _tasks.get(job_id)
    if task and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return True
    if job_id in _waiting:
        _waiting.remove(job_id)
        return True
    return False
