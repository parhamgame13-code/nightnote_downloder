"""
روت‌های عمومی وب: inspect، ساخت/پیگیری/لغو/تکرار جاب، تاریخچه، تنظیمات،
سرو فایل (/dl/) و آپلود مستقیم.

همه‌ی جاب‌های وب روی همون جدول‌های بات ساخته می‌شن (chat_id=0) تا آمار،
سقف روزانه و بن بین بات و سایت مشترک بمونه. دانلودها با رانر وب
(webapp.downloader) اجرا می‌شن، نه با صف Celery بات.
"""
import mimetypes
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from db.crud import BotConfigCRUD, DownloadJobCRUD, UserCRUD
from db.models import DownloadJob, DownloadStatus, FileType
from utils.cache_key import get_cache_hash
from utils.filename import safe_upload_filename
from utils.link_detector import LinkDetector, SiteType
from utils.metadata import MetadataExtractor
from utils.texts import get_text
from utils.url_safety import url_is_safe_to_fetch
from webapp import storage
from webapp.deps import (
    WebIdentity,
    check_rate_limit,
    get_current_web_user,
    get_db,
)
from webapp.downloader import (
    QUALITY_LABELS,
    VALID_QUALITIES,
    cancel_download,
    enqueue_download,
    get_progress,
    probe_remote_size_mb,
)

router = APIRouter()

mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("audio/mpeg", ".mp3")
mimetypes.add_type("audio/mp4", ".m4a")

# سایت‌هایی که لینک‌شون مستقیم فایله (مثل bot/main.py)
DIRECT_DOWNLOAD_SITES = (
    SiteType.GITHUB, SiteType.DIRECT_FILE, SiteType.DIRECT_AUDIO,
    SiteType.DIRECT_VIDEO, SiteType.APKPURE, SiteType.PIXELDRAIN,
    SiteType.GOFILE, SiteType.DROPBOX, SiteType.FILELU, SiteType.MEDIAFIRE,
    SiteType.GOOGLE_DRIVE, SiteType.MEGA,
)
AUDIO_SITES = (
    SiteType.SPOTIFY, SiteType.YOUTUBE_MUSIC,
    SiteType.SOUNDCLOUD, SiteType.APPLE_MUSIC,
)


# ---------------------------------------------------------------------------
# مدل‌های ورودی/خروجی
# ---------------------------------------------------------------------------

class InspectRequest(BaseModel):
    url: str = Field(..., min_length=3, max_length=2048)


class JobCreateRequest(BaseModel):
    url: str = Field(..., min_length=3, max_length=2048)
    quality: str = Field(default="best", max_length=20)
    custom_filename: Optional[str] = Field(default=None, max_length=100)
    # تایید دانلود فایل‌های بزرگ‌تر از MAX_WARN_SIZE_MB (هشدار حجم)
    confirm_large: bool = False


class SettingsUpdateRequest(BaseModel):
    spoiler_effect: Optional[bool] = None
    upload_as_document: Optional[bool] = None
    timezone: Optional[str] = Field(default=None, max_length=64)
    show_rename_option: Optional[bool] = None
    receive_screenshots: Optional[bool] = None


def _normalize_url(raw: str) -> str:
    """لینک رو از وسط متن بیرون می‌کشه و اگه scheme نداشت https:// اضافه می‌کنه."""
    extracted = LinkDetector.extract_url(raw.strip()) or raw.strip()
    if "://" not in extracted:
        extracted = "https://" + extracted
    return extracted


def _initial_file_type(site: SiteType) -> FileType:
    """نوع اولیه‌ی فایل از روی سایت (مثل bot/main.py؛ بعداً با داده‌ی واقعی دقیق می‌شه)."""
    if site in AUDIO_SITES:
        return FileType.AUDIO
    if site in DIRECT_DOWNLOAD_SITES:
        return FileType.DOCUMENT
    if site == SiteType.PINTEREST:
        return FileType.PHOTO
    return FileType.VIDEO


def _qualities_for(file_type: FileType, site: SiteType) -> list[dict]:
    """کیفیت‌های قابل انتخاب: فقط ویدیو منوی کیفیت داره (مثل بات)."""
    if file_type == FileType.VIDEO and site not in (SiteType.TWITTER, SiteType.PINTEREST):
        return [{"code": code, "label": QUALITY_LABELS[code]} for code in VALID_QUALITIES]
    return [{"code": "best", "label": QUALITY_LABELS["best"]}]


def _job_dict(job: DownloadJob, request: Request, include_progress: bool = True) -> dict:
    """سریالایز یه جاب برای API (download_url همیشه مسیر نسبی /dl/ اگه ممکن باشه)."""
    download_url = None
    if job.direct_link_token and job.file_name:
        download_url = storage.download_url_path(job.direct_link_token, job.file_name)
    elif job.direct_link_url:
        download_url = job.direct_link_url
    data = {
        "id": job.id,
        "url": job.url,
        "site": job.detected_site,
        "display_name": LinkDetector.get_display_name(job.url, SiteType(job.detected_site)) if job.detected_site in [s.value for s in SiteType] else job.detected_site,
        "file_type": job.file_type.value if job.file_type else None,
        "title": job.title,
        "artist": job.artist,
        "album": job.album,
        "duration": job.duration,
        "thumbnail_url": job.thumbnail_url,
        "description": job.description,
        "quality": job.selected_quality,
        "status": job.status.value if job.status else None,
        "file_name": job.file_name,
        "file_size_mb": job.file_size_mb,
        "error": job.error_msg,
        "download_url": download_url,
        "download_expires_at": job.direct_link_expires_at.isoformat() if job.direct_link_expires_at else None,
        "playlist_id": job.playlist_id,
        "playlist_index": job.playlist_index,
        "playlist_total": job.playlist_total,
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }
    if include_progress:
        data["progress"] = get_progress(job.id)
    return data


async def _check_gates(session: AsyncSession) -> None:
    """گیت‌های مشترک بات و وب: toggle خاموش بودن و وضعیت بحرانی دیسک."""
    enabled = await BotConfigCRUD.get_bool(session, "bot_enabled", True)
    if not enabled:
        raise HTTPException(status_code=503, detail="سرویس دانلود موقتاً خاموش است. لطفاً بعداً امتحان کن.")
    disk_state = await BotConfigCRUD.get(session, "disk_space_state") or "ok"
    if disk_state != "ok":
        raise HTTPException(status_code=503, detail="دیسک سرور پر شده و دانلودهای جدید موقتاً بسته است.")


async def _get_owned_job(session: AsyncSession, job_id: int, identity: WebIdentity) -> DownloadJob:
    job = await DownloadJobCRUD.get_by_id(session, job_id)
    if not job or job.user_id != identity.user_id:
        raise HTTPException(status_code=404, detail="این دانلود پیدا نشد.")
    return job


async def _inspect_url(url: str) -> dict:
    """
    بررسی یه لینک بدون ساخت جاب: تشخیص سایت، متادیتا، نوع فایل، کیفیت‌ها و
    هشدار حجم. منطق تشخیص نوع فایل آینه‌ی bot/main.py هست.
    """
    site = LinkDetector.detect_site(url)
    file_type = _initial_file_type(site)
    metadata = None
    title = None
    description = None
    thumbnail_url = None
    artist = None
    album = None
    duration = None

    if site in DIRECT_DOWNLOAD_SITES:
        pass  # لینک مستقیم نیازی به متادیتای yt-dlp نداره
    elif site == SiteType.TWITTER:
        from utils.twitter_downloader import fetch_tweet

        try:
            tweet_data = await fetch_tweet(url)
        except Exception as e:
            logger.warning(f"گرفتن اطلاعات توییتر (وب) ناموفق بود: {e}")
            tweet_data = None
        if not tweet_data or not tweet_data.medias:
            raise HTTPException(
                status_code=422,
                detail="نتونستم مدیایی از این لینک توییتر/ایکس پیدا کنم. ممکنه توییت فقط متنی باشه، حذف شده باشه، یا اکانتش خصوصی باشه.",
            )
        file_type = FileType.VIDEO if any(m.type == "video" for m in tweet_data.medias) else FileType.PHOTO
        title = f"@{tweet_data.author_username}" if tweet_data.author_username else tweet_data.author_name
        description = tweet_data.text or None
        artist = tweet_data.author_name or None
    elif site == SiteType.PINTEREST:
        from utils.pinterest_downloader import fetch_pinterest_pin

        try:
            pin_media = await fetch_pinterest_pin(url)
        except Exception as e:
            logger.warning(f"گرفتن اطلاعات پینترست (وب) ناموفق بود: {e}")
            pin_media = None
        if not pin_media:
            raise HTTPException(
                status_code=422,
                detail="نتونستم این پین پینترست رو پیدا کنم. ممکنه پین حذف شده باشه یا لینک اشتباه باشه.",
            )
        file_type = FileType.VIDEO if pin_media.type == "video" else FileType.PHOTO
        title = pin_media.title
        description = pin_media.title
    else:
        cookie_file = settings.INSTAGRAM_COOKIE_FILE if site in (SiteType.INSTAGRAM, SiteType.INSTAGRAM_REEL) else None
        metadata = await MetadataExtractor.extract_from_url(
            url, timeout=settings.WEB_INSPECT_TIMEOUT, cookie_file=cookie_file
        )
        if site in (SiteType.INSTAGRAM, SiteType.INSTAGRAM_REEL):
            from utils.instagram_downloader import fetch_instagram_post

            try:
                instagram_post = await fetch_instagram_post(url, cookie_file=cookie_file)
            except Exception as e:
                logger.warning(f"گرفتن اطلاعات اینستاگرام (وب) ناموفق بود: {e}")
                instagram_post = None
            if metadata is not None:
                file_type = FileType.VIDEO
            elif instagram_post and instagram_post.items:
                file_type = FileType.VIDEO if instagram_post.items[0].type == "video" else FileType.PHOTO
                description = instagram_post.caption
        if metadata:
            title = metadata.title
            artist = metadata.artist
            album = metadata.album
            duration = metadata.duration
            thumbnail_url = metadata.thumbnail_url
            description = description or metadata.description
    # اسپاتیفای: متادیتای واقعی از تگ‌های meta صفحه (دقیق‌تر از yt-dlp)
    if site == SiteType.SPOTIFY:
        from utils.spotify_downloader import fetch_spotify_track, is_playlist_or_album

        if not is_playlist_or_album(url):
            try:
                track = await fetch_spotify_track(url)
            except Exception:
                track = None
            if track:
                title = track.title
                artist = track.artist
                album = track.album
                thumbnail_url = track.cover_url

    is_playlist = LinkDetector.is_playlist(url, site)
    if site == SiteType.SPOTIFY:
        from utils.spotify_downloader import is_playlist_or_album

        is_playlist = is_playlist or is_playlist_or_album(url)
    if site == SiteType.SOUNDCLOUD and "/sets/" in url.lower():
        is_playlist = True

    size_mb = None
    size_warning = False
    if site in DIRECT_DOWNLOAD_SITES:
        size_mb = await probe_remote_size_mb(url)
        size_warning = bool(size_mb and size_mb > settings.MAX_WARN_SIZE_MB)

    return {
        "url": url,
        "site": site.value,
        "display_name": LinkDetector.get_display_name(url, site),
        "content_type": LinkDetector.get_content_type(site, is_playlist).value,
        "file_type": file_type.value,
        "is_playlist": is_playlist,
        "title": title,
        "artist": artist,
        "album": album,
        "duration": duration,
        "thumbnail_url": thumbnail_url,
        "description": description,
        "qualities": _qualities_for(file_type, site),
        "size_mb": size_mb,
        "size_warning": size_warning,
        "warn_threshold_mb": settings.MAX_WARN_SIZE_MB,
    }


# ---------------------------------------------------------------------------
# روت‌ها
# ---------------------------------------------------------------------------

@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.post("/api/inspect")
async def inspect_url(
    payload: InspectRequest,
    request: Request,
    identity: WebIdentity = Depends(get_current_web_user),
):
    """اطلاعات یه لینک رو قبل از دانلود برمی‌گردونه (بدون ساخت جاب)."""
    check_rate_limit(request)
    url = _normalize_url(payload.url)
    if not LinkDetector.is_valid_url(url):
        raise HTTPException(status_code=400, detail="لینک معتبر نیست. یه URL کامل بفرست.")
    if not await url_is_safe_to_fetch(url):
        raise HTTPException(status_code=400, detail="این لینک امن نیست و رد شد (آدرس داخلی/خصوصی).")
    return await _inspect_url(url)


@router.post("/api/jobs", status_code=201)
async def create_job(
    payload: JobCreateRequest,
    request: Request,
    identity: WebIdentity = Depends(get_current_web_user),
    session: AsyncSession = Depends(get_db),
):
    """ساخت جاب دانلود وب + صف‌بندی (با گیت‌های بات: toggle، دیسک، سقف روزانه، SSRF)."""
    check_rate_limit(request)
    await _check_gates(session)
    url = _normalize_url(payload.url)
    if not LinkDetector.is_valid_url(url):
        raise HTTPException(status_code=400, detail="لینک معتبر نیست. یه URL کامل بفرست.")
    if not await url_is_safe_to_fetch(url):
        raise HTTPException(status_code=400, detail="این لینک امن نیست و رد شد (آدرس داخلی/خصوصی).")
    quality = payload.quality or "best"
    if quality not in VALID_QUALITIES:
        raise HTTPException(status_code=400, detail="کیفیت انتخاب‌شده معتبر نیست.")

    today_count = await DownloadJobCRUD.count_today_by_user(session, identity.user_id, only_uploads=False)
    if today_count >= settings.MAX_DAILY_DOWNLOADS:
        limit_text = await get_text(session, "text_daily_limit", limit=settings.MAX_DAILY_DOWNLOADS)
        raise HTTPException(status_code=429, detail=limit_text)

    info = await _inspect_url(url)
    if info["size_warning"] and not payload.confirm_large:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"این فایل حدود {info['size_mb']:.0f} مگابایته و دانلودش ممکنه طول بکشه.",
                "size_mb": info["size_mb"],
                "requires_confirmation": True,
            },
        )
    file_type = FileType(info["file_type"])
    site = SiteType(info["site"])

    # مسیر سریع کش وب: اگه همین لینک+کیفیت قبلاً دانلود شده، بدون دانلود دوباره
    cache_hash = get_cache_hash(url, quality, file_type.value)
    cached = storage.get_cached(cache_hash)

    job = await DownloadJobCRUD.create(
        session,
        user_id=identity.user_id,
        chat_id=0,
        url=url,
        detected_site=site.value,
        raw_domain=urlparse(url).netloc.lower(),
        file_type=file_type,
        title=info["title"],
        description=info["description"],
        artist=info["artist"],
        album=info["album"],
        duration=info["duration"],
        thumbnail_url=info["thumbnail_url"],
        selected_quality=quality,
        direct_link_filename=payload.custom_filename,
    )

    if cached is not None:
        # فایل از کش: یه توکن تازه بساز و جاب رو مستقیم DONE کن
        stored = storage.store_from_cache(job.id, cache_hash, payload.custom_filename)
        if stored is not None:
            token, filename, _dest, _meta = stored
            expires_at = datetime.utcnow() + timedelta(seconds=settings.DIRECT_LINK_EXPIRE_SECONDS)
            download_url = storage.build_download_url(
                token, filename, request_base=str(request.base_url)
            )
            await DownloadJobCRUD.set_direct_link(session, job.id, token, download_url, expires_at)
            size_mb = _dest.stat().st_size / (1024 * 1024)
            await DownloadJobCRUD.update_status(
                session, job.id, DownloadStatus.DONE,
                file_name=filename, file_size_mb=round(size_mb, 2),
            )
            job = await DownloadJobCRUD.get_by_id(session, job.id)
            result = _job_dict(job, request)
            result["cached"] = True
            return result

    enqueue_download(job.id, quality, payload.custom_filename)
    job = await DownloadJobCRUD.get_by_id(session, job.id)
    result = _job_dict(job, request)
    result["cached"] = False
    return result


@router.get("/api/jobs/{job_id}")
async def get_job(
    job_id: int,
    request: Request,
    identity: WebIdentity = Depends(get_current_web_user),
    session: AsyncSession = Depends(get_db),
):
    """وضعیت یه جاب + پیشرفت زنده + فرزندهای پلی‌لیست (اگه والد باشه)."""
    job = await _get_owned_job(session, job_id, identity)
    result = _job_dict(job, request)
    # فرزندهای پلی‌لیست (playlist_id = id والد)
    stmt = (
        select(DownloadJob)
        .where(DownloadJob.playlist_id == str(job_id))
        .order_by(DownloadJob.playlist_index)
    )
    children = (await session.execute(stmt)).scalars().all()
    if children:
        result["children"] = [_job_dict(child, request) for child in children]
        done = sum(1 for c in children if c.status == DownloadStatus.DONE)
        failed = sum(1 for c in children if c.status in (DownloadStatus.FAILED, DownloadStatus.CANCELLED))
        result["playlist_progress"] = {"total": len(children), "done": done, "failed": failed}
    return result


@router.post("/api/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: int,
    request: Request,
    identity: WebIdentity = Depends(get_current_web_user),
    session: AsyncSession = Depends(get_db),
):
    """لغو یه دانلود وب (و فرزندهای پلی‌لیستش اگه والد باشه)."""
    job = await _get_owned_job(session, job_id, identity)
    if job.status not in (DownloadStatus.PENDING, DownloadStatus.DETECTING, DownloadStatus.DOWNLOADING, DownloadStatus.UPLOADING):
        raise HTTPException(status_code=409, detail="این دانلود قبلاً تمام شده و قابل لغو نیست.")
    await cancel_download(job_id)
    await DownloadJobCRUD.update_status(
        session, job_id, DownloadStatus.CANCELLED, error_msg="دانلود توسط کاربر لغو شد."
    )
    # فرزندهای فعال پلی‌لیست رو هم لغو کن
    stmt = select(DownloadJob).where(DownloadJob.playlist_id == str(job_id))
    children = (await session.execute(stmt)).scalars().all()
    for child in children:
        if child.status in (DownloadStatus.PENDING, DownloadStatus.DETECTING, DownloadStatus.DOWNLOADING, DownloadStatus.UPLOADING):
            await cancel_download(child.id)
            await DownloadJobCRUD.update_status(
                session, child.id, DownloadStatus.CANCELLED, error_msg="دانلود توسط کاربر لغو شد."
            )
    job = await DownloadJobCRUD.get_by_id(session, job_id)
    return _job_dict(job, request)


@router.post("/api/jobs/{job_id}/retry")
async def retry_job(
    job_id: int,
    request: Request,
    identity: WebIdentity = Depends(get_current_web_user),
    session: AsyncSession = Depends(get_db),
):
    """تلاش دوباره برای یه جاب ناموفق/لغوشده (با همون کیفیت و اسم فایل قبلی)."""
    job = await _get_owned_job(session, job_id, identity)
    if job.status not in (DownloadStatus.FAILED, DownloadStatus.CANCELLED):
        raise HTTPException(status_code=409, detail="فقط دانلودهای ناموفق یا لغوشده قابل تلاش دوباره هستن.")
    await _check_gates(session)
    if not await url_is_safe_to_fetch(job.url):
        raise HTTPException(status_code=400, detail="این لینک امن نیست و رد شد (آدرس داخلی/خصوصی).")
    # ریست وضعیت به PENDING (بدون دست‌زدن به متادیتا/کیفیت قبلی)
    job.status = DownloadStatus.PENDING
    job.error_msg = None
    job.started_at = None
    job.finished_at = None
    await session.commit()
    quality = job.selected_quality or "best"
    enqueue_download(job.id, quality, job.direct_link_filename)
    job = await DownloadJobCRUD.get_by_id(session, job_id)
    return _job_dict(job, request)


@router.get("/api/history")
async def history(
    request: Request,
    limit: int = Query(default=20, ge=1, le=50),
    identity: WebIdentity = Depends(get_current_web_user),
    session: AsyncSession = Depends(get_db),
):
    """تاریخچه‌ی دانلودهای کاربر وب (مثل /history بات)."""
    jobs = await DownloadJobCRUD.get_user_history(session, identity.user_id, limit=limit)
    return {"jobs": [_job_dict(job, request, include_progress=False) for job in jobs]}


@router.get("/api/settings")
async def get_settings(
    identity: WebIdentity = Depends(get_current_web_user),
):
    """
    تنظیمات شخصی کاربر وب — همون فیلدهای /settings بات:
    - spoiler_effect: تار کردن (بلور) تامبنیل‌ها تو رابط وب
    - upload_as_document: همیشه دانلود به‌صورت فایل (attachment) به‌جای پیش‌نمایش پلیر
    - timezone: نمایش زمان‌های تاریخچه به وقت محلی کاربر
    - show_rename_option: نمایش پیش‌فرض فیلد «تغییر نام فایل» قبل از دانلود
    - receive_screenshots: نمایش تامبنیل/پیش‌نمایش‌ها (خاموش = بدون تصویر)
    """
    user = identity.user
    return {
        "spoiler_effect": bool(user.spoiler_effect),
        "upload_as_document": bool(user.upload_as_document),
        "timezone": user.timezone,
        "show_rename_option": bool(user.show_rename_option),
        "receive_screenshots": True if user.receive_screenshots is None else bool(user.receive_screenshots),
    }


@router.put("/api/settings")
async def update_settings(
    payload: SettingsUpdateRequest,
    identity: WebIdentity = Depends(get_current_web_user),
    session: AsyncSession = Depends(get_db),
):
    """آپدیت تنظیمات شخصی (فقط فیلدهای پاس‌داده‌شده)."""
    fields: dict = {}
    if payload.spoiler_effect is not None:
        fields["spoiler_effect"] = payload.spoiler_effect
    if payload.upload_as_document is not None:
        fields["upload_as_document"] = payload.upload_as_document
    if payload.show_rename_option is not None:
        fields["show_rename_option"] = payload.show_rename_option
    if payload.receive_screenshots is not None:
        fields["receive_screenshots"] = payload.receive_screenshots
    if payload.timezone is not None:
        tz = payload.timezone.strip()
        if tz:
            try:
                from zoneinfo import ZoneInfo

                ZoneInfo(tz)
            except Exception:
                raise HTTPException(status_code=400, detail="نام منطقه‌ی زمانی (timezone) معتبر نیست.")
            fields["timezone"] = tz
        else:
            fields["timezone"] = None
    if not fields:
        raise HTTPException(status_code=400, detail="هیچ تنظیمی برای تغییر داده نشده.")
    user = await UserCRUD.update_settings(session, identity.telegram_id, **fields)
    if not user:
        raise HTTPException(status_code=404, detail="کاربر پیدا نشد.")
    return {
        "spoiler_effect": bool(user.spoiler_effect),
        "upload_as_document": bool(user.upload_as_document),
        "timezone": user.timezone,
        "show_rename_option": bool(user.show_rename_option),
        "receive_screenshots": True if user.receive_screenshots is None else bool(user.receive_screenshots),
    }


# ---------------------------------------------------------------------------
# سرو فایل: /dl/{token}/{filename} — آینه‌ی webserver/direct_link_server.py
# ---------------------------------------------------------------------------

@router.get("/dl/{token}/{filename}")
async def serve_download(
    token: str,
    filename: str,
    request: Request,
    preview: bool = Query(default=False),
    session: AsyncSession = Depends(get_db),
):
    """
    سرو فایل دانلودشده با پشتیبانی Range (رزومه/دانلود چندتکه) و شمارش بازدید
    فقط برای شروع‌های تازه. ?preview=1 یعنی inline (پخش تو مرورگر) به‌جای
    attachment (دانلود) — برای وقتی که کاربر upload_as_document رو خاموش کرده.
    """
    filename = unquote(filename)
    try:
        file_path = storage.resolve_served_file(token, filename)
    except ValueError:
        raise HTTPException(status_code=400, detail="درخواست نامعتبر")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="این لینک منقضی شده یا وجود نداره.")

    # اگه جابِ این توکن منقضی شده، 410 بده (فایل ممکنه هنوز رو دیسک باشه ولی
    # پاک‌سازیش عقب افتاده باشه)
    job = await DownloadJobCRUD.get_by_direct_link_token(session, token)
    if job and job.direct_link_expires_at and job.direct_link_expires_at < datetime.utcnow():
        raise HTTPException(status_code=410, detail="این لینک منقضی شده است.")

    range_header = request.headers.get("Range", "")
    is_fresh_request = not range_header or range_header.strip().startswith("bytes=0-")
    if is_fresh_request:
        try:
            await DownloadJobCRUD.increment_direct_link_hit(session, token)
        except Exception as e:
            logger.warning(f"افزایش شمارنده‌ی بازدید لینک وب {token} ناموفق بود: {e}")

    media_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    disposition = "inline" if preview else "attachment"

    # Range دستی (تک‌بازه) برای رزومه/پخش جزئی؛ بدون Range، FileResponse کامل
    if range_header.strip().startswith("bytes="):
        try:
            range_spec = range_header.strip()[6:].split(",")[0].strip()
            start_s, _, end_s = range_spec.partition("-")
            file_size = file_path.stat().st_size
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else file_size - 1
            end = min(end, file_size - 1)
            if start > end or start >= file_size:
                raise ValueError("range نامعتبر")
        except ValueError:
            raise HTTPException(status_code=416, detail="بازه‌ی درخواستی معتبر نیست.")

        def _iter_range():
            with open(file_path, "rb") as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Content-Disposition": f'{disposition}; filename="{filename}"',
        }
        return StreamingResponse(_iter_range(), status_code=206, headers=headers, media_type=media_type)

    if preview:
        # inline: filename رو پاس نمی‌دیم چون Starlette خودش Content-Disposition
        # رو attachment می‌کنه و هدر ما رو بازنویسی می‌کنه
        return FileResponse(
            path=file_path,
            media_type=media_type,
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )
    return FileResponse(path=file_path, media_type=media_type, filename=filename)


# ---------------------------------------------------------------------------
# آپلود مستقیم فایل (معادل وبِ «فایل رو به بات بفرست و لینک بگیر»)
# ---------------------------------------------------------------------------

@router.post("/api/upload", status_code=201)
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    identity: WebIdentity = Depends(get_current_web_user),
    session: AsyncSession = Depends(get_db),
):
    """
    آپلود فایل توسط خودِ کاربر → ذخیره زیر توکن + لینک دانلود مستقیم.
    سهمیه‌ی روزانه‌ی جداگانه (MAX_DAILY_DIRECT_UPLOADS) و سقف حجم
    (max_file_size_mb) مثل بات اعمال می‌شه.
    """
    check_rate_limit(request)
    await _check_gates(session)
    max_size_mb = await BotConfigCRUD.get_int(session, "max_file_size_mb", settings.MAX_FILE_SIZE_MB)

    # شمارش آپلودهای امروزِ این کاربر (web_upload + telegram_upload)
    today_start = datetime.combine(datetime.utcnow().date(), datetime.min.time())
    stmt = (
        select(DownloadJob)
        .where(
            DownloadJob.user_id == identity.user_id,
            DownloadJob.created_at >= today_start,
            DownloadJob.detected_site.in_(["web_upload", "telegram_upload"]),
            DownloadJob.status.notin_([DownloadStatus.FAILED, DownloadStatus.CANCELLED]),
        )
    )
    uploads_today = len((await session.execute(stmt)).scalars().all())
    if uploads_today >= settings.MAX_DAILY_DIRECT_UPLOADS:
        raise HTTPException(
            status_code=429,
            detail=f"سقف آپلود مستقیم روزانه‌ت پر شده ({settings.MAX_DAILY_DIRECT_UPLOADS} تا). فردا دوباره امتحان کن.",
        )

    safe_name = safe_upload_filename(file.filename or "") or "uploaded_file"
    temp_path = Path(settings.TEMP_DIR) / f"web_upload_{identity.user_id}_{int(datetime.utcnow().timestamp())}"
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    max_bytes = max_size_mb * 1024 * 1024
    written = 0
    try:
        with open(temp_path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"حجم فایل از سقف مجاز ({max_size_mb} مگابایت) بیشتر است.",
                    )
                out.write(chunk)
    except HTTPException:
        if temp_path.exists():
            temp_path.unlink()
        raise
    if written == 0:
        if temp_path.exists():
            temp_path.unlink()
        raise HTTPException(status_code=400, detail="فایل خالی است.")

    # نوع فایل از روی پسوند (برای آمار/نمایش)
    ext = Path(safe_name).suffix.lower()
    if ext in (".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".opus"):
        file_type = FileType.AUDIO
    elif ext in (".mp4", ".mkv", ".webm", ".avi", ".mov"):
        file_type = FileType.VIDEO
    elif ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        file_type = FileType.PHOTO
    else:
        file_type = FileType.DOCUMENT

    job = await DownloadJobCRUD.create(
        session,
        user_id=identity.user_id,
        chat_id=0,
        url=f"web-upload://{safe_name}",
        detected_site="web_upload",
        file_type=file_type,
        title=safe_name,
        selected_quality="best",
    )
    # اسم امن رو روی فایل موقت اعمال کن تا اسم ذخیره‌شده همون باشه
    final_temp = temp_path.with_name(safe_name)
    temp_path.rename(final_temp)
    token, filename, dest_path = storage.store_file(job.id, final_temp)
    expires_at = datetime.utcnow() + timedelta(seconds=settings.DIRECT_LINK_EXPIRE_SECONDS)
    download_url = storage.build_download_url(token, filename, request_base=str(request.base_url))
    await DownloadJobCRUD.set_direct_link(session, job.id, token, download_url, expires_at)
    size_mb = dest_path.stat().st_size / (1024 * 1024)
    await DownloadJobCRUD.update_status(
        session, job.id, DownloadStatus.DONE,
        file_name=filename, file_size_mb=round(size_mb, 2),
    )
    try:
        final_temp.unlink()
    except Exception:
        pass
    job = await DownloadJobCRUD.get_by_id(session, job.id)
    return _job_dict(job, request, include_progress=False)
