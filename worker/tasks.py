"""
Tasks برای Celery - دانلود و پردازش فایل‌ها
"""
import os
import sys
import re
import json
import html
import time
import shutil
import asyncio
import contextlib
import subprocess
from collections import namedtuple
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional

import httpx
from loguru import logger
from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import (
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReactionTypeEmoji,
    InputMediaPhoto,
    InputMediaVideo,
)

# InputRichMessage فقط تو aiogram 3.30.0+ اضافه شده. سرور فعلاً aiogram==3.29.1
# نصب داره (یا requirements.txt آپدیت نشده) که این تایپ رو نداره و باعث
# ImportError و کرش کل بات/ورکر/بیت موقع استارت می‌شد. این ایمپورت رو
# اختیاری می‌کنیم: اگه موجود بود استفاده می‌کنیم، وگرنه بی‌صدا با فال‌بک
# HTML قدیمی کار می‌کنیم (که _status_text و _StatusUpdater از قبل ساپورتش
# می‌کنن).
try:
    from aiogram.types import InputRichMessage
    RICH_MESSAGE_SUPPORTED = True
except ImportError:
    InputRichMessage = None
    RICH_MESSAGE_SUPPORTED = False
    logger.warning(
        "InputRichMessage تو نسخه‌ی نصب‌شده‌ی aiogram موجود نیست "
        "(نیاز به aiogram>=3.30.0) - نوار وضعیت با فال‌بک HTML قدیمی کار می‌کنه."
    )

from celery.signals import worker_process_init

from worker.celery_app import celery_app
from bot.config import settings, build_bot_session
from bot.premium_emoji_middleware import PremiumEmojiRequestMiddleware
from db.session import async_session_maker
from db.crud import DownloadJobCRUD, BotConfigCRUD, FileCacheCRUD, DownloadStatsCRUD, UserCRUD
from db.models import DownloadStatus, FileType
from utils.link_detector import SiteType
from utils.error_classifier import classify_download_error
from utils.url_safety import url_is_safe_to_fetch
from utils.filename import sanitize_remote_filename, safe_upload_filename
from utils.cache_key import get_cache_hash
from utils.apk_downloader import resolve_apkpure_download
from utils.cloud_downloader import (
    resolve_pixeldrain, resolve_gofile, resolve_dropbox, resolve_filelu,
    resolve_mediafire, resolve_google_drive, download_mega_file,
)
from utils.texts import get_text
from worker import concurrency


# نکته‌ی مهم: قبلاً `bot` همینجا موقع import ماژول ساخته می‌شد. چون Celery
# با pool پیش‌فرض (prefork) با os.fork() پروسه‌های worker رو می‌سازه، اون
# session/connection async (httpx) که موقع import در پروسه‌ی پدر ساخته
# می‌شد، بعد از fork به پروسه‌های فرزند کپی می‌شد. asyncio/سوکت‌های async
# فورک-سیف نیستن، پس ارسال فایل نهایی (bot.send_document/send_video/...)
# توی worker های فرزند سایلنت fail می‌کرد یا هنگ می‌کرد -> retry سه‌باره
# -> شکست بی‌صدا (حتی پیام خطا هم چون از همین bot خراب استفاده می‌کرد
# نمی‌رسید). راه‌حل: bot رو بعد از fork، جدا داخل هر پروسه‌ی worker بساز.
bot: Bot = None  # type: ignore[assignment]  # بعد از fork توسط _init_worker_bot ساخته می‌شه


@worker_process_init.connect
def _init_worker_bot(**kwargs):
    global bot
    bot = Bot(token=settings.BOT_TOKEN, session=build_bot_session())
    bot.session.middleware(PremiumEmojiRequestMiddleware())

# تارگت impersonate برای yt-dlp (نیازمند نصب curl_cffi - به requirements.txt اضافه شد)
# روی "chrome" ثابت گذاشتیم چون رایج‌ترین و پایدارترین تارگتیه که curl_cffi پشتیبانی می‌کنه
# برای دور زدن سایت‌هایی مثل PornHub که TLS fingerprint چک می‌کنن
IMPERSONATE_TARGET = "chrome"

# دامنه‌هایی که impersonate روشون *نباید* اعمال بشه.
# یوتیوب/یوتیوب‌میوزیک extractor داخلی خودشون رو دارن و impersonate کردن TLS fingerprint
# باعث می‌شه درخواست‌ها با چالش ضدبات گیر کنن یا تا سقف timeout معطل بمونن بدون خطای واضح.
# اینستاگرام هم اضافه شد: تست دستی روی سرور نشون داد بدون --impersonate (یعنی با
# urllib معمولی) دانلود موفقه، ولی با impersonate chrome (curl_cffi) اکسترکتور/API
# اینستاگرام fail می‌کنه - احتمالاً TLS fingerprint جعلی با سشن کوکی‌محورشون جور
# درنمیاد و به‌عنوان سشن نامعتبر/چالش رد می‌شه.
_IMPERSONATE_EXCLUDED_HOSTS = (
    "youtube.com", "youtu.be", "music.youtube.com",
    "instagram.com",
)


def _impersonate_args(url: str) -> list[str]:
    """آرگومان‌های --impersonate رو فقط برای سایت‌هایی که واقعاً بهش نیاز دارن برمی‌گردونه.
    برای یوتیوب خالی برمی‌گردونه."""
    if any(host in url for host in _IMPERSONATE_EXCLUDED_HOSTS):
        return []
    return ['--impersonate', IMPERSONATE_TARGET]


def _is_youtube_url(url: str) -> bool:
    """تشخیص می‌ده لینک از دامنه‌های یوتیوب (عادی/کوتاه/میوزیک) هست یا نه،
    برای فعال کردن retry با player_client جایگزین بعد از خطای 403."""
    return any(host in url for host in ("youtube.com", "youtu.be"))


_CAPTION_TITLE_MAX_CHARS = 80


def _build_caption_header_and_body(job) -> tuple[str, str]:
    """هدرِ بولدِ caption (safe_title) و بدنه‌ی توضیحات (caption_body) رو باهم
    می‌سازه و برمی‌گردونه.

    چرا باهم؟ چون تیک‌تاک/پینترست (و مشابه‌ها) اصلاً مفهوم «عنوان کوتاه»
    ندارن - چیزی که به‌عنوان title از این سایت‌ها می‌رسه (مثلاً title از
    tikwm.com، یا title از API پینترست) در واقع کپشن کاملِ پسته، که می‌تونه
    صدها کاراکتر و پر از هشتگ باشه. قبلاً این متن کامل عیناً تو هدر بولدِ
    caption (<b>...</b>) می‌رفت - یه دیوار متن بولدِ زشت/بی‌ریخت، دقیقاً همون
    «مشکل کپشن» که تو تیک‌تاک و پینترست دیده می‌شد. حالا هدر رو کوتاه
    می‌کنیم و مطمئن می‌شیم متنِ کامل (چه از description بیاد چه خودِ title
    طولانی باشه) جایی تو بدنه از دست نره."""
    raw_title = (job.title or "بدون عنوان").strip()
    if len(raw_title) > _CAPTION_TITLE_MAX_CHARS:
        short_title = raw_title[:_CAPTION_TITLE_MAX_CHARS].rstrip() + "…"
    else:
        short_title = raw_title
    safe_title = html.escape(short_title)

    description = (job.description or "").strip()
    header_was_truncated = short_title != raw_title

    if description and description != raw_title:
        # توضیحاتِ جدا از title داریم (مثلاً توییتر/اینستاگرام) - همون رو نشون بده
        body = f"\n{html.escape(description)}\n"
    elif header_was_truncated:
        # description جدایی نداریم (یا دقیقاً با title یکیه)، ولی هدر بریده شده -
        # پس متن کامل (description یا خودِ title) رو تو بدنه نشون بده تا گم نشه
        body = f"\n{html.escape(description or raw_title)}\n"
    else:
        body = ""
    return safe_title, body


def _remove_job_dir(job_id: int):
    """حذف پوشه مؤقت یک job بلافاصله بعد از اتمام کار (موفق یا ناموفق)
    تا فضای دیسک سرور با تجمع فایل‌های دانلودشده پر نشه"""
    try:
        temp_dir = Path(settings.TEMP_DIR) / str(job_id)
        if temp_dir.exists():
            import shutil
            shutil.rmtree(temp_dir)
            logger.info(f"پوشه مؤقت job {job_id} پاک شد")
    except Exception as e:
        logger.error(f"خطا در پاک‌سازی پوشه مؤقت job {job_id}: {e}")


def _classify_download_error(err) -> Optional[str]:
    """Wrapper نازک - منطق واقعی تو utils/error_classifier.py هست (بدون
    وابستگی به Celery/DB) تا بشه مستقل تستش کرد."""
    return classify_download_error(err)


async def _notify_capacity_timeout(job_id: int):
    """
    وقتی یه job بیش از حد منتظر آزاد شدن یه اسلات دانلود همزمان می‌مونه
    (یعنی MAX_CONCURRENT_DOWNLOADS برای مدت طولانی پر بوده)، به‌جای اینکه
    برای همیشه تو صف بمونه، به کاربر اطلاع می‌دیم که سرور شلوغه.
    """
    async with async_session_maker() as session:
        job = await DownloadJobCRUD.get_by_id(session, job_id)
        if not job:
            return
        await DownloadJobCRUD.update_status(
            session, job_id, DownloadStatus.FAILED,
            error_msg="بیش از حد در صف دانلود همزمان منتظر ظرفیت خالی موند"
        )
        logger.warning(f"job {job_id} به‌خاطر پر بودن طولانی‌مدت ظرفیت دانلود همزمان لغو شد")
        await DownloadStatsCRUD.increment_today(session, successful=False, site=job.detected_site)
        await _notify_failure(
            job.chat_id, job_id,
            "⏳ سرور الان بیش از حد شلوغه و نوبت دانلودت خیلی طول کشید. لطفاً چند دقیقه دیگه دوباره امتحان کن.",
            message_id=job.message_id,
        )
        await _delete_status_message(job)
        _remove_job_dir(job_id)


async def _notify_failure(chat_id: int, job_id: int, reason: str, message_id: int | None = None, tech_detail: str | None = None):
    """
    اطلاع‌رسانی به کاربر توی تلگرام وقتی دانلود یا ارسال فایل شکست می‌خوره.
    قبلاً وقتی جاب FAILED می‌شد فقط تو دیتابیس ثبت می‌شد و کاربر هیچ پیامی
    نمی‌گرفت (نه توی صف دانلود نه توی صف آپلود) - این تابع همون خلأ رو پر می‌کنه.
    اگه tech_detail داده بشه، جزئیات فنی خام (مثل خروجی yt-dlp) پشت
    اسپویلر (<tg-spoiler>) مخفی می‌شه تا چت شلوغ نشه ولی کاربر کنجکاو بتونه
    با یه لمس ببینتش.
    """
    try:
        async with async_session_maker() as _text_session:
            text = await get_text(_text_session, "text_failed_wrapper", reason=reason)
        if tech_detail:
            text += f"\n\n🔍 <b>جزئیات فنی:</b> <tg-spoiler>{html.escape(tech_detail[:300])}</tg-spoiler>"
        retry_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔁 تلاش دوباره", callback_data=f"retry_dl:{job_id}", style="primary")
        ]])
        # افکت 👎 - فقط تو چت خصوصی کار می‌کنه (طبق خودِ Bot API)
        _fail_effect = {"message_effect_id": "5104858069142078462"} if chat_id > 0 else {}
        try:
            await bot.send_message(
                chat_id,
                text,
                parse_mode="HTML",
                reply_markup=retry_keyboard,
                **_fail_effect,
            )
        except TelegramRetryAfter as e:
            # پیام شکست مهمه که به کاربر برسه - یه بار طبق retry_after
            # تلگرام صبر می‌کنیم و دوباره امتحان می‌کنیم
            wait = e.retry_after + 1
            logger.warning(f"Rate limit موقع اطلاع‌رسانی شکست (job {job_id})، {wait} ثانیه صبر می‌کنیم")
            await asyncio.sleep(wait)
            await bot.send_message(
                chat_id,
                text,
                parse_mode="HTML",
                reply_markup=retry_keyboard,
                **_fail_effect,
            )
        except Exception as e:
            if _fail_effect and "effect" in str(e).lower():
                # اگه خودِ افکت رد شد، بدون افکت دوباره امتحان کن - رسیدن پیام مهم‌تره
                await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=retry_keyboard)
            else:
                raise
    except Exception as e:
        logger.error(f"خطا در اطلاع‌رسانی شکست به کاربر (job {job_id}): {e}")

    # ری‌اکشن 👎 روی پیام اصلی کاربر بذاریم تا بفهمه لینکش با خطا مواجه شده
    if message_id:
        try:
            await bot.set_message_reaction(
                chat_id,
                message_id,
                reaction=[ReactionTypeEmoji(emoji="👎")],
            )
        except Exception as e:
            logger.warning(f"گذاشتن ری‌اکشن شکست روی پیام اصلی job {job_id} ممکن نشد: {e}")


async def _delete_status_message(job):
    """پاک کردن پیام «کیفیت ✅ / در حال دانلود...» بعد از اتمام کار (چه موفق چه ناموفق)
    تا چت یا گروپ شلوغ نشه."""
    if not job or not getattr(job, "status_message_id", None):
        return
    try:
        await bot.delete_message(job.chat_id, job.status_message_id)
    except Exception as e:
        logger.warning(f"حذف پیام وضعیت job {job.id} ناموفق بود: {e}")


async def _send_direct_link_message(job_id: int, chat_id: int, reply_to: int | None, token: str, url: str):
    """ارسال پیام لینک دانلود مستقیم همراه با دکمه‌های تمدید/آمار/QR (تابع مشترک،
    جایگزین کد تکراری تو download_file و download_uploaded_file)."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 تمدید", callback_data=f"dl_renew:{token}"),
            InlineKeyboardButton(text="📊 آمار بازدید", callback_data=f"dl_stats:{token}"),
            InlineKeyboardButton(text="📷 QR", callback_data=f"dl_qr:{token}"),
        ],
        [InlineKeyboardButton(text="🗑 حذف فایل از سرور", callback_data=f"dl_delete:{token}")],
    ])
    try:
        await bot.send_message(
            chat_id,
            f"🔗 <b>لینک دانلود مستقیم:</b>\n<code>{html.escape(url)}</code>\n\n"
            f"⏳ این لینک تا <b>{settings.DIRECT_LINK_EXPIRE_SECONDS // 3600} ساعت</b> دیگه معتبره.",
            parse_mode="HTML",
            reply_to_message_id=reply_to,
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.warning(f"ارسال پیام لینک مستقیم برای job {job_id} ناموفق بود: {e}")


async def _download_with_progress(
    file_id: str, destination: Path, total_bytes: float | None,
    status: "_StatusUpdater", details: str,
):
    """دانلود فایل کاربر از تلگرام با گزارش پیشرفت واقعی.

    نسخه‌ی قبلی این تابع bot.download() رو تو یه asyncio.Task جدا اجرا
    می‌کرد و موازی باهاش سایز فایل مقصد رو poll می‌کرد - ولی با Local Bot
    API Server کار نمی‌کرد: وقتی get_file صدا زده بشه، سرور محلی
    file_path رو به‌صورت یه مسیر مطلق روی دیسک خودش برمی‌گردونه (نه یه
    URL نسبی)، و aiogram توی این حالت به‌جای دانلود HTTP یه کپی مستقیم از
    دیسک انجام می‌ده که ظاهراً هیچ‌وقت به حلقه‌ی موازی ما فرصت اجرا نمی‌داد
    (نوار همیشه رو ۰٪ یخ می‌زد، بدون توجه به سایز فایل).

    برای اینکه دیگه به رفتار داخلی aiogram وابسته نباشیم، خودمون مستقیم
    مدیریتش می‌کنیم:
      - Local Bot API Server (file_path یه مسیر واقعی رو دیسکه): کپی رو
        تو یه ترد جدا (executor) انجام می‌دیم و هر ثانیه از رو یه متغیر
        مشترک، پیشرفت واقعی رو می‌خونیم و پیام رو آپدیت می‌کنیم.
      - غیر این حالت (Cloud API یا local server‌ای که URL می‌ده): با
        httpx استریم می‌کنیم و پیشرفت رو همون تو حلقه‌ی خوندن چانک‌ها
        گزارش می‌دیم (دقیقاً همون روشی که _download_generic_file داره).
    """
    try:
        t0 = time.monotonic()

        # موازی با انتظار برای get_file، هر چند ثانیه یه پیام «در حال
        # آماده‌سازی...» با زمان سپری‌شده نشون می‌دیم - تا کاربر نبینه نوار
        # وضعیت رو صفر یخ زده (این دقیقاً همون چیزی بود که تا الان اتفاق
        # می‌افتاد: این مرحله می‌تونست چند دقیقه طول بکشه بدون هیچ گزارشی).
        waiting_done = {"done": False}

        async def _waiting_ping():
            tick = 0
            while not waiting_done["done"]:
                await asyncio.sleep(5.0)
                if waiting_done["done"]:
                    break
                tick += 1
                elapsed = int(time.monotonic() - t0)
                await status.update(
                    _status_text(
                        "⏳", "در حال آماده‌سازی فایل روی سرورهای تلگرام...",
                        note=f"{elapsed} ثانیه از انتظار گذشته", details=details,
                    )
                )

        ping_task = asyncio.create_task(_waiting_ping())
        try:
            file_info = await asyncio.wait_for(
                bot.get_file(file_id), timeout=settings.TELEGRAM_GET_FILE_TIMEOUT_SECONDS
            )
        finally:
            waiting_done["done"] = True
            ping_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await ping_task

        logger.info(
            f"get_file برای job (file_id={file_id[:12]}...) تو {time.monotonic()-t0:.1f}s برگشت: "
            f"file_path={file_info.file_path!r} file_size={file_info.file_size}"
        )
    except asyncio.TimeoutError:
        logger.error(
            f"get_file برای file_id={file_id[:12]}... بیش از "
            f"{settings.TELEGRAM_GET_FILE_TIMEOUT_SECONDS}s طول کشید، لغو شد"
        )
        raise RuntimeError(
            "آماده‌سازی فایل روی سرورهای تلگرام بیش از حد طول کشید. لطفاً دوباره امتحان کن."
        )
    except Exception as e:
        logger.warning(f"bot.get_file برای {file_id} ناموفق بود ({e})، فال‌بک به bot.download بدون نوار پیشرفت")
        await bot.download(file_id, destination=destination)
        return

    remote_path = file_info.file_path
    total = total_bytes or (file_info.file_size or None)

    async def _report_loop(get_copied, is_done):
        last_shown = -1
        last_mb_shown = -1.0
        tick = 0
        while not is_done():
            await asyncio.sleep(1.0)
            tick += 1
            copied = get_copied()
            if tick <= 3 or tick % 10 == 0:
                logger.info(f"_download_with_progress tick={tick} copied={copied} total={total}")
            if total and total > 0:
                percent = min(99.0, copied / total * 100)
                if int(percent) != last_shown:
                    last_shown = int(percent)
                    await status.update(
                        _status_text("⬇️", "در حال دریافت فایل از تلگرام", percent=percent, details=details)
                    )
            else:
                mb = copied / (1024 * 1024)
                if mb - last_mb_shown >= 0.5:
                    last_mb_shown = mb
                    await status.update(
                        _status_text(
                            "⬇️", "در حال دریافت فایل از تلگرام",
                            note=f"{mb:.1f} مگابایت دریافت شد", details=details,
                        )
                    )

    if remote_path and os.path.isfile(remote_path):
        # حالت Local Bot API Server: فایل از قبل رو دیسک خودمونه، فقط کپی می‌کنیم
        logger.info(f"مسیر local پیدا شد ({remote_path})، کپی مستقیم رو دیسک شروع شد")
        progress = {"copied": 0}

        def _copy_sync():
            with open(remote_path, "rb") as src, open(destination, "wb") as dst:
                while True:
                    chunk = src.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
                    progress["copied"] += len(chunk)

        loop = asyncio.get_event_loop()
        copy_future = loop.run_in_executor(None, _copy_sync)
        await asyncio.gather(
            copy_future,
            _report_loop(lambda: progress["copied"], lambda: copy_future.done()),
        )
        logger.info(f"کپی محلی تموم شد، {progress['copied']} بایت نوشته شد")

        # همین که خودمون یه کپی کامل از فایل داریم، نسخه‌ی Local Bot API
        # Server دیگه کاملاً زائده - منتظر پاک‌سازی ساعتیِ cleanup_temp_files
        # (که بر اساس BOT_API_LOCAL_FILES_MAX_AGE_HOURS کار می‌کنه، پیش‌فرض
        # ۱۲ ساعت) نمی‌مونیم؛ فوراً پاکش می‌کنیم. دقیقاً همین تاخیر (فایل‌های
        # ۱-۲ گیگی که ساعت‌ها دوبل رو دیسک می‌موندن) باعث پر شدن دیسک سرور
        # و از کار افتادن Redis/کل صف شد.
        try:
            os.remove(remote_path)
            logger.info(f"نسخه‌ی تکراری Local Bot API Server پاک شد: {remote_path}")
        except OSError as e:
            logger.warning(f"پاک کردن نسخه‌ی تکراری Local Bot API Server ({remote_path}) ناموفق بود: {e}")
        return

    # حالت Cloud API / local server‌ای که URL نسبی می‌ده: استریم با httpx
    base = settings.LOCAL_BOT_API_URL if settings.USE_LOCAL_BOT_API_SERVER else "https://api.telegram.org"
    file_url = f"{base}/file/bot{settings.BOT_TOKEN}/{remote_path}"
    logger.info(f"remote_path محلی نبود، استریم با httpx از {file_url}")

    progress = {"copied": 0}
    done_flag = {"done": False}

    async def _stream_download():
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=None)) as client:
            async with client.stream("GET", file_url) as response:
                response.raise_for_status()
                with open(destination, "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                        f.write(chunk)
                        progress["copied"] += len(chunk)
        done_flag["done"] = True

    stream_task = asyncio.create_task(_stream_download())
    try:
        await asyncio.gather(
            stream_task,
            _report_loop(lambda: progress["copied"], lambda: done_flag["done"]),
        )
    finally:
        done_flag["done"] = True


def _url_matches_domain(url: str, domains: list) -> bool:
    """چک می‌کنه هاست یه URL دقیقاً یکی از دامنه‌های لیست هست یا زیردامنه‌ی یکیشون"""
    try:
        host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
        if host.startswith("www."):
            host = host[4:]
    except Exception:
        return False

    for d in domains:
        d = (d or "").lower().strip()
        if d.startswith("www."):
            d = d[4:]
        if not d:
            continue
        if host == d or host.endswith("." + d):
            return True
    return False


async def _get_self_destruct_config(session):
    """
    تنظیمات «خودتخریب فایل» رو می‌خونه: لیست دامنه‌هایی که فایل‌های
    دانلودشده ازشون، بعد از ارسال، خودکار پاک می‌شن + بعد از چند ثانیه.
    این فقط برای دامنه‌هایی که ادمین صریحاً تو /selfdestruct اضافه کرده
    اعمال می‌شه - نه یوتیوب/اینستا/تیک‌تاک/آهنگ و بقیه‌ی سایت‌های عادی.
    """
    domains_raw = await BotConfigCRUD.get(session, "self_destruct_domains")
    seconds_raw = await BotConfigCRUD.get(session, "self_destruct_seconds")
    try:
        domains = json.loads(domains_raw) if domains_raw else []
    except Exception:
        domains = []
    try:
        seconds = int(seconds_raw) if seconds_raw else 90
    except Exception:
        seconds = 90
    return domains, seconds


def _progress_bar(percent: float, length: int = 14) -> str:
    """رسم نوار پیشرفت به سبک مونواسپیس، مثلاً: ▓▓▓▓▓▓░░░░░░░░  42.0%"""
    percent = max(0.0, min(100.0, percent))
    filled = int(length * percent / 100)
    return f"{'▓' * filled}{'░' * (length - filled)}  {percent:5.1f}%"


# خروجی _status_text: هم نسخه‌ی HTML قدیمی (فال‌بک) و هم نسخه‌ی Rich Message
# (Bot API 10.1 - core.telegram.org/bots/api#rich-messages، پشتیبانی شده در
# aiogram 3.29+ از طریق InputRichMessage و edit_message_text(rich_message=...))
_StatusPayload = namedtuple("_StatusPayload", ["html", "rich"])


def _status_text(
    emoji: str,
    title: str,
    percent: Optional[float] = None,
    note: Optional[str] = None,
    details: Optional[str] = None,
) -> _StatusPayload:
    """
    یه پیام وضعیت شیک می‌سازه: تیتر heading، نوار پیشرفت مونواسپیس، یه خط
    توضیح، جداکننده و جزئیات تکمیلی داخل بلاک <details> تاشو (فرمت Rich
    Message جدید تلگرام). هم‌زمان نسخه‌ی قدیمی HTML رو هم برمی‌گردونه که اگه
    Rich Message به هر دلیلی (مثلاً یه رفتار مستندنشده‌ی API خیلی تازه) رد
    شد، بی‌صدا به همون فرمت قبلی که همیشه کار می‌کرد فال‌بک کنیم.
    """
    # --- نسخه‌ی قدیمی HTML (فال‌بک) ---
    html_lines = [f"{emoji} <b>{html.escape(title)}</b>"]
    if percent is not None:
        html_lines.append(f"<code>{_progress_bar(percent)}</code>")
    if note:
        html_lines.append(f"<i>{html.escape(note)}</i>")
    if details:
        html_lines.append(f"<blockquote expandable>{details}</blockquote>")
    html_text = "\n".join(html_lines)

    # --- نسخه‌ی Rich Message (Rich Markdown) ---
    md_parts = [f"## {emoji} {title}"]
    if percent is not None:
        md_parts.append(f"`{_progress_bar(percent)}`")
    if note:
        md_parts.append(f"_{note}_")
    if details:
        md_parts.append("---")
        md_parts.append(f"<details>\n<summary>جزئیات</summary>\n\n{details}\n\n</details>")
    md_text = "\n\n".join(md_parts)
    rich = InputRichMessage(markdown=md_text, is_rtl=True) if RICH_MESSAGE_SUPPORTED else None

    return _StatusPayload(html=html_text, rich=rich)


class _StatusUpdater:
    """
    مسئول ادیت کردن پیام «در حال دانلود...» با نوار وضعیت زنده و فرمت HTML.
    برای اینکه به Rate limit تلگرام نخوریم و برای هر تغییر ۱٪ یه درخواست
    API نزنیم، ادیت‌ها throttle می‌شن (حداقل فاصله‌ی زمانی بین دو ادیت).

    نکته‌ی مهم: edit_message_text اگه reply_markup صریح پاس داده نشه، دکمه‌ی
    «❌ لغو»‌ای که موقع شروع کار زیر پیام گذاشته بودیم رو حذف می‌کنه - یعنی
    از همون اولین آپدیت نوار پیشرفت، کاربر دیگه هیچ راهی برای لغو کردن
    نداشت (دقیقاً همون چیزی که تو اسکرین‌شات‌ها هم دیده می‌شد: پیام گیر کرده
    بدون هیچ دکمه‌ای). برای همین هر ادیت، همون کیبورد لغو رو دوباره می‌فرسته.
    """

    def __init__(self, job, min_interval: float = 3.0):
        self.job = job
        self.min_interval = min_interval
        self._last_edit_at = 0.0
        self._last_text: Optional[str] = None
        self._cancel_keyboard = (
            InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ لغو", callback_data=f"cancel_dl:{job.id}", style="danger")
            ]])
            if job else None
        )

    async def update(self, payload, force: bool = False):
        if not self.job or not getattr(self.job, "status_message_id", None):
            return

        # سازگاری با فراخوانی‌های قدیمی که مستقیم رشته‌ی HTML بدن به‌جای
        # _StatusPayload (احتیاطی، فعلاً همه‌ی فراخوانی‌ها از _status_text میان)
        if isinstance(payload, str):
            html_text, rich = payload, None
        else:
            html_text, rich = payload.html, payload.rich

        if html_text == self._last_text:
            return
        now = time.monotonic()
        if not force and (now - self._last_edit_at) < self.min_interval:
            return

        async def _send_edit():
            if rich is not None:
                try:
                    await bot.edit_message_text(
                        chat_id=self.job.chat_id,
                        message_id=self.job.status_message_id,
                        rich_message=rich,
                        reply_markup=self._cancel_keyboard,
                    )
                    return
                except TelegramRetryAfter:
                    raise
                except Exception as e:
                    # Rich Message برای هر دلیلی رد شد (نسخه‌ی سرور،
                    # سینتکس، هرچی) - فوری فال‌بک به HTML قدیمی که همیشه
                    # جواب می‌داده؛ نوار وضعیت هیچ‌وقت نباید بشکنه
                    logger.debug(
                        f"ادیت Rich Message برای job {self.job.id} رد شد، فال‌بک به HTML: {e}"
                    )
            await bot.edit_message_text(
                html_text,
                chat_id=self.job.chat_id,
                message_id=self.job.status_message_id,
                parse_mode="HTML",
                reply_markup=self._cancel_keyboard,
            )

        try:
            try:
                await _send_edit()
            except TelegramRetryAfter as e:
                # یه ادیت نوار پیشرفت حیاتی نیست، ولی یه بار طبق retry_after
                # صبر می‌کنیم که چت واقعاً بی‌فیدبک نمونه؛ اگه بازم رد شد ولش می‌کنیم
                wait = e.retry_after + 1
                logger.debug(f"Rate limit ادیت وضعیت job {self.job.id}، {wait}s صبر می‌کنیم")
                await asyncio.sleep(wait)
                await _send_edit()
            self._last_edit_at = now
            self._last_text = html_text
        except Exception as e:
            # اگه متن فرقی نکرده باشه یا پیام پاک شده باشه، مهم نیست - ولی
            # چون قبلاً این فقط debug بود (که با LOG_LEVEL=INFO پیش‌فرض کلاً
            # هیچ‌جا نوشته نمی‌شد)، یه شکست مداوم/مکرر تو ادیت‌ها کاملاً بی‌صدا
            # می‌موند و نوار وضعیت بدون هیچ اثری تو لاگ یخ می‌زد. warning چون
            # واقعاً یه چیزیه که ارزش دیدن داره، نه یه نویز بی‌اهمیت.
            logger.warning(f"ادیت نوار وضعیت job {self.job.id} ناموفق: {type(e).__name__}: {e}")


class _ProgressFSInputFile(FSInputFile):
    """
    نسخه‌ی سفارشی FSInputFile که موقع آپلود، به‌جای اینکه فقط یه پیام ثابت
    «در حال آپلود...» نشون بده، با شمردن بایت‌های واقعاً خونده‌شده (همونی
    که aiogram موقع ساختن بدنه‌ی multipart برای send_document/send_video/...
    داخلی صداش می‌زنه) درصد واقعی آپلود رو گزارش می‌ده.

    توجه: امضای واقعی FSInputFile.read() تو aiogram اینه: read(self, bot).
    (نه chunk_size که تو نسخه‌ی قبلی این کلاس اشتباهاً فرض شده بود - اون
    نسخه به‌طور اتفاقی کار می‌کرد چون آرگومان bot که پوزیشنال پاس داده
    می‌شه رو با پارامتر chunk_size اشتباه می‌گرفت، ولی شکننده بود.)
    """

    def __init__(self, path, on_progress=None, filename=None, chunk_size: int = 64 * 1024):
        super().__init__(path, filename=filename, chunk_size=chunk_size)
        self._on_progress = on_progress
        try:
            self._total_size = os.path.getsize(path)
        except OSError:
            self._total_size = 0

    async def read(self, bot):
        if not self._on_progress or not self._total_size:
            async for chunk in super().read(bot):
                yield chunk
            return

        sent = 0
        last_report = 0.0
        async for chunk in super().read(bot):
            sent += len(chunk)
            now = time.monotonic()
            if now - last_report >= 2.0 or sent >= self._total_size:
                last_report = now
                percent = min(100.0, sent / self._total_size * 100)
                asyncio.create_task(self._on_progress(percent))
            yield chunk


_YTDLP_PERCENT_RE = re.compile(r'\[download\]\s+(\d{1,3}(?:\.\d+)?)%')
# خط کامل پیشرفت yt-dlp با --newline معمولاً این شکلیه:
#   [download]  45.2% of   10.00MiB at    1.20MiB/s ETA 00:08
# سرعت و ETA همیشه نیستن (مثلاً وقتی سایز کل معلوم نیست می‌نویسه
# "at Unknown speed ETA Unknown")، برای همین هر دو گروه اختیاری‌ان.
_YTDLP_SPEED_ETA_RE = re.compile(
    r'\[download\].*?\bat\s+([\d.]+\s?\w+/s|Unknown speed)(?:\s+ETA\s+(\S+))?'
)

# وقتی stdout یه پروسه به‌جای ترمینال به یه pipe وصل باشه (دقیقاً حالت
# ما اینجا)، خودِ پایتونِ داخل yt-dlp به‌طور پیش‌فرض stdout رو
# block-buffered می‌کنه، نه line-buffered - یعنی هر خط progress رو فوری
# فلاش نمی‌کنه و می‌ذاره تو بافر جمع بشه (چند KB). نتیجه‌ش این بود که
# نوار وضعیت دقیقه‌ها رو یه عدد (معمولاً ۰٪) یخ می‌زد، بعد یهو یه‌دفعه چند
# تا خط progress با هم می‌رسیدن. `stdbuf -oL -eL` این بافرینگ رو مجبور
# می‌کنه سطح OS خط‌به‌خط باشه - مستقل از اینکه خودِ yt-dlp/پایتونش چیکار
# می‌خواد بکنه. اگه stdbuf رو سیستم نبود (خیلی بعیده رو یه لینوکس با
# coreutils)، فال‌بک می‌کنیم به PYTHONUNBUFFERED که برای اسکریپت‌های
# پایتونی (حالت معمولِ نصب yt-dlp با pip) هم کافیه.
_STDBUF_PATH = shutil.which("stdbuf")


async def _run_ytdlp_with_progress(
    cmd: list[str], status: _StatusUpdater, timeout: int, details: Optional[str] = None
):
    """
    اجرای yt-dlp به‌صورت async (به‌جای subprocess.run که تا آخر کار هیچ
    فیدبکی نمی‌ده)، با خوندن خط‌به‌خط stdout برای استخراج درصد پیشرفت
    و بروزرسانی زنده‌ی پیام وضعیت. خروجی و رفتار (returncode/stderr) با
    subprocess.run سازگاره تا بقیه‌ی کد نیازی به تغییر نداشته باشه.
    """
    full_cmd = [*cmd, '--newline']
    if _STDBUF_PATH:
        full_cmd = [_STDBUF_PATH, '-oL', '-eL', *full_cmd]

    env = {**os.environ, "PYTHONUNBUFFERED": "1"}

    proc = await asyncio.create_subprocess_exec(
        *full_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    stderr_chunks: list[bytes] = []
    last_activity = {"t": time.monotonic(), "percent": 0.0}

    async def _read_stdout():
        async for raw_line in proc.stdout:
            line = raw_line.decode(errors="ignore").strip()
            m = _YTDLP_PERCENT_RE.search(line)
            if m:
                percent = float(m.group(1))
                last_activity["t"] = time.monotonic()
                last_activity["percent"] = percent

                # سرعت/ETA رو هم از همون خط استخراج می‌کنیم تا کاربر بدونه
                # چقدر طول می‌کشه، نه فقط درصد خام (که برای فایل‌های بزرگ
                # بدون این اطلاعات گیج‌کننده‌ست - معلوم نیست نوار داره تند
                # پیش می‌ره یا ساعت‌ها طول می‌کشه)
                note = None
                se = _YTDLP_SPEED_ETA_RE.search(line)
                if se:
                    speed, eta = se.group(1), se.group(2)
                    if speed and speed != "Unknown speed":
                        note = f"سرعت: {speed}"
                        if eta and eta != "Unknown":
                            note += f" | باقی‌مانده: {eta}"

                await status.update(
                    _status_text("⬇️", "در حال دانلود", percent=percent, note=note, details=details)
                )
            elif "[Merger]" in line or "Merging formats" in line:
                last_activity["t"] = time.monotonic()
                await status.update(
                    _status_text("🔧", "در حال ادغام ویدیو و صدا...", percent=100, details=details),
                    force=True,
                )
            elif "[ExtractAudio]" in line:
                last_activity["t"] = time.monotonic()
                await status.update(
                    _status_text("🎧", "در حال تبدیل به MP3...", percent=100, details=details),
                    force=True,
                )

    async def _read_stderr():
        async for raw_line in proc.stderr:
            stderr_chunks.append(raw_line)

    async def _stall_watchdog():
        # حتی با فیکس بافرینگ، ممکنه واقعاً چند ده ثانیه هیچ خط progress
        # جدیدی از yt-dlp نیاد (مثلاً سرعت شبکه واقعاً کنده). به‌جای اینکه
        # کاربر فکر کنه گیر کرده/کرش کرده، هر چند ثانیه یه پیام زنده
        # نشون می‌دیم که هنوز در حال کاره - با آخرین درصدی که داشتیم.
        while True:
            await asyncio.sleep(5.0)
            idle = time.monotonic() - last_activity["t"]
            if idle >= 15.0:
                await status.update(
                    _status_text(
                        "⬇️", "در حال دانلود", percent=last_activity["percent"],
                        note=f"{int(idle)} ثانیه از آخرین گزارش گذشته", details=details,
                    )
                )

    watchdog_task = asyncio.create_task(_stall_watchdog())
    try:
        await asyncio.wait_for(
            asyncio.gather(_read_stdout(), _read_stderr(), proc.wait()),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        stderr_text = b"".join(stderr_chunks).decode(errors="ignore")
        return 1, (stderr_text + "\nخطا: دانلود بیش از حد مجاز طول کشید (timeout)")
    finally:
        watchdog_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await watchdog_task

    stderr_text = b"".join(stderr_chunks).decode(errors="ignore")
    return proc.returncode, stderr_text


async def _probe_soundcloud_playlist_tracks(url: str) -> Optional[list[str]]:
    """با --flat-playlist می‌پرسه این لینک ساندکلاود یه ترک تکیه یا پلی‌لیست/ست.

    برخلاف اینکه بشه از روی خودِ متن URL فهمید (لینک‌های کوتاه‌شده‌ی اپ
    موبایل مثل on.soundcloud.com/xxxx هیچ نشونه‌ای تو مسیرشون ندارن)، این
    تابع واقعاً از yt-dlp می‌پرسه. اگه فقط یه ترک بود، یا probe fail/timeout
    شد، None برمی‌گردونه (یعنی: caller باید مسیر تک‌فایل معمولی رو بره).
    """
    try:
        list_cmd = ['yt-dlp', '--flat-playlist', '--print', '%(webpage_url)s', url]
        list_proc = await asyncio.create_subprocess_exec(
            *list_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, _ = await asyncio.wait_for(list_proc.communicate(), timeout=45)
        except asyncio.TimeoutError:
            list_proc.kill()
            await list_proc.wait()
            return None

        if list_proc.returncode != 0:
            return None

        track_urls = [
            line.strip() for line in stdout_bytes.decode(errors="ignore").splitlines()
            if line.strip()
        ]
        return track_urls or None
    except Exception as e:
        logger.debug(f"probe پلی‌لیست ساندکلاود برای {url[:60]}... ناموفق بود: {e}")
        return None


def run_async(coro):
    """اجرای async تابع در sync context"""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    return loop.run_until_complete(coro)


async def _download_generic_file(
    url: str, temp_dir: Path, status: Optional["_StatusUpdater"] = None, details: Optional[str] = None,
    extra_headers: Optional[dict] = None,
) -> Optional[Path]:
    """
    دانلود مستقیم فایل (برای لینک‌هایی مثل گیت‌هاب که ویدیو/صوت نیستن و
    yt-dlp پشتیبانیشون نمی‌کنه). با httpx استریم می‌کنه تا کل فایل حجیم
    یک‌جا توی حافظه لود نشه. اگه status داده بشه، پیام وضعیت با درصد
    (یا حجم دانلودشده وقتی سرور Content-Length نمی‌ده) زنده آپدیت می‌شه.

    ریدایرکت‌ها دستی دنبال می‌شن و مقصد هر hop قبل از درخواست دوباره
    SSRF-check می‌شه (url_is_safe_to_fetch: شامل resolve DNS هم هست) -
    follow_redirects=True یعنی یه لینک عمومی که 302 به 169.254.169.254 یا
    127.0.0.1 می‌داد، بدون هیچ چکی توسط سرور fetch می‌شد.
    """
    # خیلی از CDNها (مخصوصاً اون‌هایی که لینک‌شون توکن‌دار/سایمن‌ده - s=/e=/node=)
    # درخواست‌های بدون User-Agent مرورگر رو 403 می‌کنن. بدون این هدر،
    # raise_for_status پایین اکسپشن می‌ندازه و کل دانلود fail می‌شه.
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
    }
    if extra_headers:
        headers.update(extra_headers)

    filename = None
    file_path: Optional[Path] = None
    async with httpx.AsyncClient(follow_redirects=False, timeout=60.0, headers=headers) as client:
        current_url = url
        for _hop in range(10):
            if not await url_is_safe_to_fetch(current_url):
                raise RuntimeError(
                    "لینک به یه آدرس داخلی/خصوصی سرور اشاره می‌کنه و دانلودش رد شد."
                )
            async with client.stream("GET", current_url) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise RuntimeError("سرور ریدایرکت بدون مقصد برگردوند.")
                    current_url = str(response.url.join(location))
                    continue

                response.raise_for_status()

                # تلاش برای گرفتن اسم فایل از هدر Content-Disposition
                content_disposition = response.headers.get("content-disposition", "")
                match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', content_disposition)
                if match:
                    filename = match.group(1).strip()

                if not filename:
                    # از روی مسیر URL نهایی (بعد از ریدایرکت‌ها) اسم بساز
                    final_path = str(response.url).split("?")[0]
                    candidate = final_path.rsplit("/", 1)[-1]
                    filename = candidate or "downloaded_file"
                filename = sanitize_remote_filename(filename) or "downloaded_file"

                file_path = temp_dir / filename
                total = int(response.headers.get("content-length") or 0)
                downloaded = 0

                with open(file_path, "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if status:
                            if total:
                                percent = downloaded / total * 100
                                await status.update(
                                    _status_text("⬇️", "در حال دانلود", percent=percent, details=details)
                                )
                            else:
                                mb = downloaded / (1024 * 1024)
                                await status.update(
                                    _status_text(
                                        "⬇️", "در حال دانلود", note=f"{mb:.1f} مگابایت دانلود شد...", details=details
                                    )
                                )
                break
        else:
            # ۱۰ hop پشت‌سرهم ریدایرکت بدون رسیدن به محتوای واقعی
            raise RuntimeError("لینک بین بیش از ۱۰ ریدایرکت حرکت کرد (حلقه‌ی ریدایرکت).")

    return file_path


async def _download_hls_with_ffmpeg(hls_url: str, temp_dir: Path, filename: str = "video.mp4") -> Optional[Path]:
    """
    دانلود یه استریم HLS (.m3u8) و mux کردنش به یه mp4 معمولی، با ffmpeg.
    برخلاف _download_generic_file (که فقط یه GET ساده می‌زنه)، این تابع برای
    لینک‌های .m3u8 لازمه - چون خودِ فایل .m3u8 فقط یه playlist متنیه که به
    سگمنت‌های واقعی ویدیو اشاره می‌کنه؛ httpx به‌تنهایی نمی‌تونه سگمنت‌ها رو
    دانلود و به هم بچسبونه. (برای Story Pin/Idea Pin پینترست لازم شد، چون
    ویدیوی این نوع پین‌ها همیشه HLS ئه، نه mp4 مستقیم.)
    """
    output_path = temp_dir / filename
    try:
        result = subprocess.run(
            [
                'ffmpeg', '-y',
                '-i', hls_url,
                '-c', 'copy',
                '-bsf:a', 'aac_adtstoasc',
                str(output_path),
            ],
            capture_output=True,
            timeout=180,
        )
        if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
            return output_path
        logger.error(f"دانلود HLS با ffmpeg ناموفق بود: {result.stderr.decode(errors='ignore')[:500]}")
    except Exception as e:
        logger.error(f"خطا در دانلود HLS با ffmpeg: {e}")
    return None


def _generate_thumbnail(video_path: Path, temp_dir: Path) -> Optional[Path]:
    """
    ساخت تامبنیل (عکس پیش‌نمایش) از ویدیو با ffmpeg، تا تلگرام صفحه‌ی سیاه
    نشون نده و قبل از پخش، عکس واقعی از ویدیو دیده بشه.
    """
    thumb_path = temp_dir / "thumb.jpg"

    # اول از ثانیه ۱ امتحان می‌کنیم، اگه ویدیو کوتاه‌تر از این بود از فریم اول
    for seek_time in ("00:00:01.000", "00:00:00.000"):
        try:
            result = subprocess.run(
                [
                    'ffmpeg', '-y',
                    '-ss', seek_time,
                    '-i', str(video_path),
                    '-vframes', '1',
                    '-vf', 'scale=320:-2',
                    str(thumb_path),
                ],
                capture_output=True,
                timeout=30,
            )
            if result.returncode == 0 and thumb_path.exists() and thumb_path.stat().st_size > 0:
                return thumb_path
        except Exception as e:
            logger.warning(f"ساخت تامبنیل ناموفق ({seek_time}): {e}")

    return None


def _prepare_video_note(video_path: Path, temp_dir: Path) -> Optional[Path]:
    """
    تبدیل ویدیوی دانلودشده به فرمت «پیام ویدیویی» (گرد) تلگرام:
    - کراپ مربعی از وسط فریم (چون تلگرام فقط ویدیوی مربعی رو به‌صورت گرد نشون می‌ده)
    - ریسایز به 384x384 (برای حجم و سرعت ارسال مناسب)
    - محدود کردن به حداکثر 60 ثانیه (محدودیت پیام‌های ویدیویی تلگرام)
    """
    output_path = temp_dir / "video_note.mp4"
    try:
        result = subprocess.run(
            [
                'ffmpeg', '-y',
                '-i', str(video_path),
                '-t', '60',
                '-vf', "crop='min(iw,ih)':'min(iw,ih)',scale=384:384",
                '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
                '-c:a', 'aac', '-b:a', '128k',
                '-movflags', '+faststart',
                str(output_path),
            ],
            capture_output=True,
            timeout=180,
        )
        if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
            return output_path
        logger.error(f"خطا در ساخت پیام ویدیویی: {result.stderr}")
    except Exception as e:
        logger.error(f"خطا در ساخت پیام ویدیویی: {e}")
    return None


@celery_app.task(bind=True, queue='download')
def download_file(self, job_id: int, quality: str = "best"):
    """دانلود فایل"""
    logger.info(f"شروع دانلود job {job_id} با کیفیت {quality}")

    # قبل از شروع، منتظر یه اسلات خالی از سقف MAX_CONCURRENT_DOWNLOADS می‌مونیم.
    # بدون این، اگه مثلاً ۵۰۰ نفر همزمان لینک بفرستن، همه‌شون همزمان
    # yt-dlp/ffmpeg رو استارت می‌کنن و کل CPU/پهنای‌باند سرور اشغال می‌شه.
    limit = settings.MAX_CONCURRENT_DOWNLOADS
    poll_interval = 3
    max_wait = settings.DOWNLOAD_TIMEOUT  # بیشتر از این یعنی صف واقعاً گیر کرده
    waited = 0
    while not concurrency.try_acquire(job_id, limit):
        if waited == 0:
            logger.info(f"ظرفیت دانلود همزمان پره (سقف {limit})، job {job_id} منتظر موند")
        if waited >= max_wait:
            run_async(_notify_capacity_timeout(job_id))
            return None
        time.sleep(poll_interval)
        waited += poll_interval

    async def _download():
        async with async_session_maker() as session:
            job = await DownloadJobCRUD.get_by_id(session, job_id)
            if not job:
                return None

            if job.status == DownloadStatus.CANCELLED:
                logger.info(f"job {job_id} قبل از شروع لغو شده بود، رد شد")
                return None

            # بروزرسانی وضعیت
            await DownloadJobCRUD.update_status(
                session, job_id, DownloadStatus.DOWNLOADING
            )

            # نوار وضعیت زنده - همون پیام «در حال دانلود...» رو با درصد پیشرفت آپدیت می‌کنه
            status = _StatusUpdater(job)
            _details = (
                f"🌐 سایت: {html.escape(job.detected_site or '-')}\n"
                f"🎚 کیفیت: {html.escape(quality)}\n"
                f"🔗 لینک: {html.escape(job.url[:70])}{'…' if len(job.url) > 70 else ''}"
            )
            await status.update(
                _status_text("⬇️", "در حال دانلود", percent=0, details=_details), force=True
            )

            try:
                # ایجاد پوشه مؤقت
                temp_dir = Path(settings.TEMP_DIR) / str(job_id)
                temp_dir.mkdir(parents=True, exist_ok=True)

                # این متغیر رو هر شاخه (توییتر/پینترست/اینستاگرام/تیک‌تاک/yt-dlp
                # عمومی) موقع موفقیت پر می‌کنه، تا تو لاگ نهایی دقیقاً معلوم باشه
                # فایل از کدوم روش دانلود شده - قبلاً همه‌جا فقط "دانلود موفق"
                # می‌نوشت و برای فهمیدن مسیر واقعی باید کل لاگ رو دستی می‌گشتی.
                download_method = "yt-dlp"

                if job.detected_site == SiteType.TWITTER.value:
                    # مسیر مخصوص توییتر/ایکس - بدون yt-dlp، مستقیم از GraphQL توییتر
                    # (چون این‌روزها اکسترکتور توییتر تو yt-dlp خیلی ناپایداره)
                    try:
                        from utils.twitter_downloader import fetch_tweet

                        tweet = await fetch_tweet(job.url)
                        if not tweet or not tweet.medias:
                            raise RuntimeError("مدیایی برای این توییت پیدا نشد")

                        wanted_type = "video" if job.file_type == FileType.VIDEO else "photo"
                        media = next((m for m in tweet.medias if m.type == wanted_type), tweet.medias[0])

                        download_method = "twitter-graphql"
                        file_path = await _download_generic_file(media.url, temp_dir, status=status, details=_details)

                        # لینک‌های مدیای توییتر معمولاً تو مسیرشون پسوند درست دارن،
                        # ولی برای اطمینان، اگه پسوند نداشت دستی اضافه می‌کنیم
                        if file_path and not file_path.suffix:
                            ext = ".mp4" if media.type == "video" else ".jpg"
                            new_path = file_path.with_suffix(ext)
                            file_path.rename(new_path)
                            file_path = new_path
                    except Exception as e:
                        await DownloadJobCRUD.update_status(
                            session, job_id, DownloadStatus.FAILED,
                            error_msg=f"خطا در دانلود از توییتر: {str(e)[:255]}"
                        )
                        logger.error(f"خطا در دانلود توییتر برای job {job_id}: {e}")
                        await DownloadStatsCRUD.increment_today(session, successful=False, site=job.detected_site)
                        _reason = _classify_download_error(e) or (
                            "دانلود از توییتر/X با خطا مواجه شد. ممکنه توییت حذف شده، خصوصی باشه یا "
                            "توییتر موقتاً محدودیت گذاشته باشه."
                        )
                        await _notify_failure(
                            job.chat_id, job_id,
                            _reason,
                            message_id=job.message_id,
                            tech_detail=str(e),
                        )
                        await _delete_status_message(job)
                        _remove_job_dir(job_id)
                        return None

                    if not file_path or not file_path.exists():
                        await DownloadJobCRUD.update_status(
                            session, job_id, DownloadStatus.FAILED,
                            error_msg="فایلی پیدا نشد"
                        )
                        logger.error(f"فایلی برای job {job_id} بعد از دانلود توییتر پیدا نشد")
                        await DownloadStatsCRUD.increment_today(session, successful=False, site=job.detected_site)
                        await _notify_failure(
                            job.chat_id, job_id,
                            "دانلود از توییتر به مشکل خورد و چیزی روی سرور پیدا نشد.",
                            message_id=job.message_id
                        )
                        await _delete_status_message(job)
                        _remove_job_dir(job_id)
                        return None

                elif job.detected_site == SiteType.PINTEREST.value:
                    # مسیر مخصوص پینترست - بدون yt-dlp (اکسترکتورش پین عکسی رو
                    # پشتیبانی نمی‌کنه و برای ویدیو هم گاهی ۴۰۴ تقلبی می‌ده)،
                    # مستقیم لینک واقعیِ عکس/ویدیوی پین رو می‌گیریم و دانلود می‌کنیم.
                    try:
                        from utils.pinterest_downloader import fetch_pinterest_pin

                        pin_media = await fetch_pinterest_pin(job.url)
                        if not pin_media:
                            raise RuntimeError("مدیایی برای این پین پیدا نشد")

                        download_method = f"pinterest-direct ({pin_media.type}{'/hls' if pin_media.is_hls else ''})"
                        if pin_media.is_hls:
                            # Story Pin/Idea Pin - لینک .m3u8 ئه، با httpx معمولی
                            # قابل دانلود نیست، باید با ffmpeg mux بشه
                            await status.update(
                                _status_text("⬇️", "در حال دانلود ویدیوی Story Pin...", details=_details),
                                force=True,
                            )
                            file_path = await _download_hls_with_ffmpeg(pin_media.url, temp_dir)
                        else:
                            file_path = await _download_generic_file(
                                pin_media.url, temp_dir, status=status, details=_details
                            )
                        if file_path and not file_path.suffix:
                            ext = ".mp4" if pin_media.type == "video" else ".jpg"
                            new_path = file_path.with_suffix(ext)
                            file_path.rename(new_path)
                            file_path = new_path

                        if pin_media.title and not job.title:
                            await DownloadJobCRUD.update_status(
                                session, job_id, DownloadStatus.DOWNLOADING, title=pin_media.title
                            )
                    except Exception as e:
                        await DownloadJobCRUD.update_status(
                            session, job_id, DownloadStatus.FAILED,
                            error_msg=f"خطا در دانلود از پینترست: {str(e)[:255]}"
                        )
                        logger.error(f"خطا در دانلود پین پینترست برای job {job_id}: {e}")
                        await DownloadStatsCRUD.increment_today(session, successful=False, site=job.detected_site)
                        _reason = _classify_download_error(e) or (
                            "نتونستم این پین رو از پینترست دانلود کنم. ممکنه پین حذف شده یا "
                            "لینک منقضی شده باشه."
                        )
                        await _notify_failure(
                            job.chat_id, job_id,
                            _reason,
                            message_id=job.message_id,
                            tech_detail=str(e),
                        )
                        await _delete_status_message(job)
                        _remove_job_dir(job_id)
                        return None

                    if not file_path or not file_path.exists():
                        await DownloadJobCRUD.update_status(
                            session, job_id, DownloadStatus.FAILED,
                            error_msg="فایلی پیدا نشد"
                        )
                        logger.error(f"فایلی برای job {job_id} بعد از دانلود پین پینترست پیدا نشد")
                        await DownloadStatsCRUD.increment_today(session, successful=False, site=job.detected_site)
                        await _notify_failure(
                            job.chat_id, job_id,
                            "دانلود از پینترست به مشکل خورد و چیزی روی سرور پیدا نشد.",
                            message_id=job.message_id
                        )
                        await _delete_status_message(job)
                        _remove_job_dir(job_id)
                        return None

                elif (
                    job.detected_site in (SiteType.INSTAGRAM.value, SiteType.INSTAGRAM_REEL.value)
                    and job.file_type == FileType.PHOTO
                ):
                    # پست عکسی/کاروسل اینستاگرام - yt-dlp این‌ها رو پشتیبانی
                    # نمی‌کنه (فقط ویدیو/ریلز)، برای همین مستقیم از API داخلی
                    # اینستاگرام می‌گیریم. اگه موزیک پس‌زمینه هم داشته باشه،
                    # جدا دانلود و به‌عنوان فایل صوتی جدا ارسال می‌شه.
                    try:
                        from utils.instagram_downloader import fetch_instagram_post

                        ig_post = await fetch_instagram_post(job.url, cookie_file=settings.INSTAGRAM_COOKIE_FILE)
                        if not ig_post or not ig_post.items:
                            raise RuntimeError("مدیایی برای این پست اینستاگرام پیدا نشد")

                        if len(ig_post.items) > 1:
                            # کاروسل چند‌آیتمی - چون یه job فقط یه فایل اصلی رو از طریق
                            # صفِ آپلود معمولی می‌فرسته، همه‌ی آیتم‌ها رو همین‌جا دانلود
                            # و یه‌جا به‌صورت آلبوم (media group) می‌فرستیم
                            downloaded = []
                            for idx, sub_item in enumerate(ig_post.items[:10]):  # سقف تلگرام برای آلبوم: ۱۰ آیتم
                                try:
                                    sub_path = await _download_generic_file(
                                        sub_item.url, temp_dir, status=status, details=_details
                                    )
                                    if sub_path and not sub_path.suffix:
                                        ext = ".mp4" if sub_item.type == "video" else ".jpg"
                                        renamed = sub_path.with_name(f"{sub_path.name}_{idx}{ext}")
                                        sub_path.rename(renamed)
                                        sub_path = renamed
                                    if sub_path and sub_path.exists():
                                        downloaded.append((sub_item.type, sub_path))
                                except Exception as e:
                                    logger.warning(
                                        f"دانلود آیتم {idx} کاروسل اینستاگرام ناموفق بود (job {job_id}): {e}"
                                    )

                            if not downloaded:
                                raise RuntimeError("هیچ‌کدوم از آیتم‌های کاروسل دانلود نشدن")

                            safe_title, caption_body = _build_caption_header_and_body(job)
                            album_caption = f"<b>🖼 {safe_title}</b>\n{caption_body}"

                            media_group = []
                            for i, (m_type, m_path) in enumerate(downloaded):
                                cap = album_caption if i == 0 else None
                                cap_kwargs = {"caption": cap, "parse_mode": "HTML"} if cap else {}
                                if m_type == "video":
                                    media_group.append(InputMediaVideo(media=FSInputFile(str(m_path)), **cap_kwargs))
                                else:
                                    media_group.append(InputMediaPhoto(media=FSInputFile(str(m_path)), **cap_kwargs))

                            await bot.send_media_group(job.chat_id, media=media_group)

                            if job.message_id:
                                try:
                                    await bot.set_message_reaction(
                                        job.chat_id, job.message_id,
                                        reaction=[ReactionTypeEmoji(emoji="👍")],
                                    )
                                except Exception as e:
                                    logger.warning(f"گذاشتن ری‌اکشن برای job {job_id} ممکن نشد: {e}")

                            # موزیک پس‌زمینه (اگه پست داشته باشه)
                            if ig_post.music_url:
                                try:
                                    music_path = await _download_generic_file(
                                        ig_post.music_url, temp_dir, status=status, details=_details
                                    )
                                    if music_path and music_path.exists():
                                        music_caption = (
                                            f"🎵 {html.escape(ig_post.music_title)}" if ig_post.music_title else None
                                        )
                                        await bot.send_audio(
                                            job.chat_id,
                                            FSInputFile(str(music_path)),
                                            caption=music_caption,
                                            parse_mode="HTML" if music_caption else None,
                                        )
                                except Exception as e:
                                    logger.warning(
                                        f"دانلود/ارسال موزیک پس‌زمینه‌ی پست اینستاگرام ناموفق بود (job {job_id}): {e}"
                                    )

                            total_size_mb = sum(p.stat().st_size for _, p in downloaded) / (1024 * 1024)
                            await DownloadJobCRUD.update_status(
                                session, job_id, DownloadStatus.DONE, file_size_mb=total_size_mb
                            )
                            await DownloadStatsCRUD.increment_today(
                                session, successful=True, size_mb=total_size_mb, site=job.detected_site
                            )
                            logger.info(f"✅ آلبوم اینستاگرام ارسال شد (job {job_id}, {len(downloaded)} آیتم)")
                            await _delete_status_message(job)
                            _remove_job_dir(job_id)
                            return "album_sent"

                        main_item = ig_post.items[0]
                        download_method = f"instagram-api ({main_item.type})"
                        file_path = await _download_generic_file(
                            main_item.url, temp_dir, status=status, details=_details
                        )
                        if file_path and not file_path.suffix:
                            ext = ".mp4" if main_item.type == "video" else ".jpg"
                            new_path = file_path.with_suffix(ext)
                            file_path.rename(new_path)
                            file_path = new_path

                        # موزیک پس‌زمینه (اگه پست داشته باشه) - چون یه job فقط یه
                        # فایل اصلی داره، این رو جدا از صفِ آپلود معمولی، همین‌جا
                        # به‌عنوان یه پیام صوتی اضافه می‌فرستیم
                        if ig_post.music_url:
                            try:
                                music_path = await _download_generic_file(
                                    ig_post.music_url, temp_dir, status=status, details=_details
                                )
                                if music_path and music_path.exists():
                                    music_caption = f"🎵 {html.escape(ig_post.music_title)}" if ig_post.music_title else None
                                    await bot.send_audio(
                                        job.chat_id,
                                        FSInputFile(str(music_path)),
                                        caption=music_caption,
                                        parse_mode="HTML" if music_caption else None,
                                    )
                            except Exception as e:
                                logger.warning(
                                    f"دانلود/ارسال موزیک پس‌زمینه‌ی پست اینستاگرام ناموفق بود (job {job_id}): {e}"
                                )
                    except Exception as e:
                        await DownloadJobCRUD.update_status(
                            session, job_id, DownloadStatus.FAILED,
                            error_msg=f"خطا در دانلود پست اینستاگرام: {str(e)[:255]}"
                        )
                        logger.error(f"خطا در دانلود پست عکسی اینستاگرام برای job {job_id}: {e}")
                        await DownloadStatsCRUD.increment_today(session, successful=False, site=job.detected_site)
                        _reason = _classify_download_error(e) or (
                            "نتونستم این پست اینستاگرام رو دانلود کنم. ممکنه پست حذف شده، خصوصی "
                            "باشه یا اینستاگرام موقتاً محدودیت گذاشته باشه."
                        )
                        await _notify_failure(
                            job.chat_id, job_id,
                            _reason,
                            message_id=job.message_id,
                            tech_detail=str(e),
                        )
                        await _delete_status_message(job)
                        _remove_job_dir(job_id)
                        return None

                    if not file_path or not file_path.exists():
                        await DownloadJobCRUD.update_status(
                            session, job_id, DownloadStatus.FAILED,
                            error_msg="فایلی پیدا نشد"
                        )
                        logger.error(f"فایلی برای job {job_id} بعد از دانلود پست اینستاگرام پیدا نشد")
                        await DownloadStatsCRUD.increment_today(session, successful=False, site=job.detected_site)
                        await _notify_failure(
                            job.chat_id, job_id,
                            "دانلود از اینستاگرام به مشکل خورد و چیزی روی سرور پیدا نشد.",
                            message_id=job.message_id
                        )
                        await _delete_status_message(job)
                        _remove_job_dir(job_id)
                        return None

                elif job.file_type == FileType.DOCUMENT:
                    # لینک‌های مستقیم فایل (گیت‌هاب و مشابه) - yt-dlp نمی‌فهمدشون
                    try:
                        download_url = job.url
                        apk_title = None
                        extra_headers = None
                        if job.detected_site == SiteType.APKPURE.value:
                            # این یکی لینک صفحه‌ی اپه، نه خودِ فایل؛ اول باید لینک
                            # مستقیم دانلود رو از تو HTML صفحه پیدا کنیم.
                            await status.update(
                                _status_text("🔍", "در حال پیدا کردن لینک مستقیم دانلود...", details=_details),
                                force=True,
                            )
                            apk_info = await resolve_apkpure_download(job.url)
                            if not apk_info:
                                raise RuntimeError(
                                    "لینک مستقیم دانلود پیدا نشد (احتمالاً ساختار صفحه‌ی "
                                    "APKPure عوض شده یا اپ حذف شده)"
                                )
                            download_url = apk_info.download_url
                            apk_title = apk_info.title

                        elif job.detected_site == SiteType.PIXELDRAIN.value:
                            await status.update(
                                _status_text("🔍", "در حال پیدا کردن فایل...", details=_details), force=True,
                            )
                            pd_info = await resolve_pixeldrain(job.url)
                            if not pd_info:
                                raise RuntimeError("فایل روی Pixeldrain پیدا نشد (احتمالاً حذف شده یا لینک اشتباهه)")
                            download_url = pd_info.download_url
                            apk_title = pd_info.title

                        elif job.detected_site == SiteType.DROPBOX.value:
                            db_info = resolve_dropbox(job.url)
                            if not db_info:
                                raise RuntimeError("لینک Dropbox قابل تبدیل به لینک مستقیم نبود")
                            download_url = db_info.download_url
                            apk_title = db_info.title

                        elif job.detected_site == SiteType.GOFILE.value:
                            await status.update(
                                _status_text("🔍", "در حال خوندن محتوای پوشه‌ی Gofile...", details=_details), force=True,
                            )
                            gf_files = await resolve_gofile(job.url)
                            if not gf_files:
                                raise RuntimeError(
                                    "محتوای این لینک Gofile پیدا نشد (ممکنه پوشه پسورد داشته باشه یا API سایت عوض شده باشه)"
                                )
                            if len(gf_files) > 1:
                                logger.warning(
                                    f"لینک Gofile job {job_id} یه پوشه با {len(gf_files)} فایله؛ "
                                    f"فعلاً فقط اولین فایل دانلود می‌شه (ارسال چندفایلی هنوز پیاده نشده)."
                                )
                            gf_info = gf_files[0]
                            download_url = gf_info.download_url
                            apk_title = gf_info.title
                            extra_headers = gf_info.extra_headers

                        elif job.detected_site == SiteType.FILELU.value:
                            await status.update(
                                _status_text("🔍", "در حال گرفتن لینک مستقیم از FileLu...", details=_details), force=True,
                            )
                            fl_info = await resolve_filelu(job.url, settings.FILELU_API_KEY)
                            if not fl_info:
                                raise RuntimeError(
                                    "دانلود از FileLu ناموفق بود (کلید API تنظیم نشده، اعتبارش تموم شده، یا فایل حذف شده)"
                                )
                            download_url = fl_info.download_url
                            apk_title = fl_info.title

                        elif job.detected_site == SiteType.MEDIAFIRE.value:
                            await status.update(
                                _status_text("🔍", "در حال پیدا کردن لینک مستقیم دانلود...", details=_details), force=True,
                            )
                            mf_info = await resolve_mediafire(job.url)
                            if not mf_info:
                                raise RuntimeError("لینک مستقیم دانلود از MediaFire پیدا نشد (احتمالاً فایل حذف شده)")
                            download_url = mf_info.download_url
                            apk_title = mf_info.title

                        elif job.detected_site == SiteType.GOOGLE_DRIVE.value:
                            await status.update(
                                _status_text("🔍", "در حال آماده‌سازی لینک Google Drive...", details=_details), force=True,
                            )
                            gd_info = await resolve_google_drive(job.url)
                            if not gd_info:
                                raise RuntimeError("لینک Google Drive قابل پردازش نبود (شاید private باشه)")
                            download_url = gd_info.download_url
                            apk_title = gd_info.title

                        elif job.detected_site == SiteType.MEGA.value:
                            # MEGA فرق داره: چون فایل رمزنگاری‌شده‌ست، خودِ mega.py هم‌زمان
                            # دانلود و رمزگشایی می‌کنه و مستقیم فایل رو تو temp_dir می‌ذاره
                            # (نه اینکه یه لینک بده تا _download_generic_file دانلودش کنه).
                            await status.update(
                                _status_text("⬇️", "در حال دانلود و رمزگشایی از MEGA...", details=_details), force=True,
                            )
                            mega_info = await download_mega_file(job.url, str(temp_dir))
                            if not mega_info:
                                raise RuntimeError(
                                    "دانلود از MEGA ناموفق بود (لینک نامعتبره یا کتابخونه‌ی mega.py نصب نیست)"
                                )
                            file_path = Path(mega_info.download_url)  # این‌جا مسیر فایله نه URL
                            apk_title = mega_info.title
                            download_url = None  # دیگه نیازی به _download_generic_file نیست

                        if download_url is not None:
                            file_path = await _download_generic_file(
                                download_url, temp_dir, status=status, details=_details, extra_headers=extra_headers,
                            )
                        # اگه download_url خالی مونده (فقط برای MEGA اتفاق می‌افته)، یعنی
                        # file_path از قبل (تو branch مربوط به MEGA بالا) مستقیم ست شده.

                        if apk_title and not job.title:
                            await DownloadJobCRUD.update_status(
                                session, job_id, DownloadStatus.DOWNLOADING, title=apk_title
                            )
                    except Exception as e:
                        await DownloadJobCRUD.update_status(
                            session, job_id, DownloadStatus.FAILED,
                            error_msg=f"خطا در دانلود فایل: {str(e)[:255]}"
                        )
                        logger.exception(f"خطا در دانلود فایل مستقیم (job {job_id}, url={job.url}): {e}")
                        await DownloadStatsCRUD.increment_today(session, successful=False, site=job.detected_site)
                        _reason = _classify_download_error(e) or (
                            "نتونستم لینک رو دانلود کنم. ممکنه لینک منقضی شده یا از دسترس خارج شده باشه."
                        )
                        await _notify_failure(
                            job.chat_id, job_id,
                            _reason,
                            message_id=job.message_id,
                            tech_detail=str(e),
                        )
                        await _delete_status_message(job)
                        _remove_job_dir(job_id)
                        return None

                    if not file_path or not file_path.exists():
                        await DownloadJobCRUD.update_status(
                            session, job_id, DownloadStatus.FAILED,
                            error_msg="فایلی پیدا نشد"
                        )
                        logger.error(f"فایلی برای job {job_id} بعد از دانلود مستقیم پیدا نشد")
                        await DownloadStatsCRUD.increment_today(session, successful=False, site=job.detected_site)
                        await _notify_failure(
                            job.chat_id, job_id,
                            "دانلود فایل به مشکل خورد و چیزی روی سرور پیدا نشد.",
                            message_id=job.message_id
                        )
                        await _delete_status_message(job)
                        _remove_job_dir(job_id)
                        return None

                elif job.detected_site == SiteType.SPOTIFY.value:
                    # اسپاتیفای DRM داره - نمی‌شه مستقیم ازش صدا گرفت. متادیتای
                    # واقعی (اسم/خواننده/کاور) رو از تگ‌های meta خودِ صفحه
                    # می‌گیریم (utils/spotify_downloader.py)، و خودِ صدا رو با
                    # جست‌وجوی همون آهنگ تو یوتیوب دانلود می‌کنیم.
                    try:
                        from utils.spotify_downloader import (
                            fetch_spotify_track, is_playlist_or_album,
                            build_search_query, tag_audio_file,
                        )

                        if is_playlist_or_album(job.url):
                            from utils.spotify_downloader import fetch_spotify_playlist_tracks

                            track_urls = await fetch_spotify_playlist_tracks(job.url)
                            if not track_urls:
                                raise RuntimeError(
                                    "نتونستم لیست ترک‌های این پلی‌لیست/آلبوم رو از اسپاتیفای "
                                    "بگیرم. ممکنه لینک خصوصی/نامعتبر باشه یا اسپاتیفای صفحه‌ش "
                                    "رو عوض کرده باشه."
                                )

                            # هر ترک یه job جدا و مستقل می‌شه (دقیقاً همون مسیر تک‌آهنگ
                            # بالا رو طی می‌کنه)، چون کل معماری این پروژه بر پایه‌ی
                            # «یه job = یه فایل»ـه، نه دسته‌ای. برای اینکه بشه پیشرفت
                            # کلیِ پلی‌لیست رو (X از N دانلود شد) بعداً جمع زد، همه‌ی
                            # این job‌های فرزند یه playlist_id مشترک (خودِ id این
                            # job والد) می‌گیرن.
                            total_tracks = len(track_urls)
                            playlist_batch_id = str(job_id)
                            spawned = 0
                            for idx, track_url in enumerate(track_urls, start=1):
                                try:
                                    sub_status_msg = await bot.send_message(
                                        job.chat_id, "⏳ تو صف دانلود...",
                                        reply_to_message_id=job.message_id or None,
                                    )
                                    sub_job = await DownloadJobCRUD.create(
                                        session,
                                        user_id=job.user_id,
                                        chat_id=job.chat_id,
                                        message_id=job.message_id,
                                        status_message_id=sub_status_msg.message_id,
                                        url=track_url,
                                        detected_site=SiteType.SPOTIFY.value,
                                        file_type=FileType.AUDIO,
                                        playlist_id=playlist_batch_id,
                                        playlist_index=idx,
                                        playlist_total=total_tracks,
                                    )
                                    download_file.delay(sub_job.id, "best")
                                    spawned += 1
                                except Exception as e:
                                    logger.warning(
                                        f"ساخت job جدا برای ترک {track_url} "
                                        f"(پلی‌لیست job {job_id}) ناموفق بود: {e}"
                                    )

                            # خودِ job والد کاری برای دانلود نداره - فقط پیام وضعیتش
                            # رو به یه گزارش زنده‌ی «X/N دانلود شد» تبدیل می‌کنیم که
                            # poll_playlist_progress دوره‌ای آپدیتش می‌کنه.
                            await DownloadJobCRUD.update_status(session, job_id, DownloadStatus.DONE)
                            if job.status_message_id and spawned > 0:
                                try:
                                    await bot.edit_message_text(
                                        f"🎧 {spawned} تا آهنگ از این پلی‌لیست/آلبوم پیدا شد، "
                                        f"شروع دانلود...\n{_progress_bar(0)}",
                                        chat_id=job.chat_id, message_id=job.status_message_id,
                                    )
                                except Exception:
                                    pass
                                poll_playlist_progress.apply_async(
                                    args=[job_id, playlist_batch_id], countdown=8
                                )
                            elif job.status_message_id:
                                try:
                                    await bot.edit_message_text(
                                        "❌ هیچ‌کدوم از ترک‌های این پلی‌لیست/آلبوم قابل صف‌کردن نبودن.",
                                        chat_id=job.chat_id, message_id=job.status_message_id,
                                    )
                                except Exception:
                                    pass
                            _remove_job_dir(job_id)
                            return None

                        track = await fetch_spotify_track(job.url)
                        if not track:
                            raise RuntimeError("اطلاعات این آهنگ از اسپاتیفای گرفته نشد")

                        # صدای واقعی رو از یوتیوب (با جست‌وجوی نام آهنگ+خواننده)
                        # دانلود می‌کنیم - اسپاتیفای DRM داره و نمی‌شه مستقیم ازش
                        # صدا گرفت. تگ‌های فایل نهایی با متادیتای واقعیِ اسپاتیفای
                        # (نه عنوان خامِ ویدیوی یوتیوب) جایگزین می‌شن.
                        search_query = build_search_query(track)
                        outtmpl = str(temp_dir / '%(title)s.%(ext)s')
                        cmd = [
                            'yt-dlp',
                            f'ytsearch1:{search_query} audio',
                            '-x', '--audio-format', 'mp3', '--audio-quality', '0',
                            '-o', outtmpl, '--no-playlist',
                        ]
                        returncode, stderr_text = await _run_ytdlp_with_progress(
                            cmd, status, settings.DOWNLOAD_TIMEOUT, details=_details
                        )

                        # همون باگی که قبلاً پیدا شد: چون job.url خودِ لینک
                        # اسپاتیفایه (نه یوتیوب)، این مسیر وارد بلوک retry عمومی
                        # پایین‌تر (که فقط با _is_youtube_url(job.url) فعال می‌شه)
                        # نمی‌شه؛ برای همین زنجیره‌ی player_client رو مستقل اینجا
                        # هم تکرار می‌کنیم.
                        if returncode != 0 and "403" in (stderr_text or ""):
                            for client in ("mweb", "android", "tv"):
                                logger.warning(
                                    f"job {job_id}: جست‌وجوی یوتیوب برای آهنگ اسپاتیفای با "
                                    f"403 جواب داد، تلاش دوباره با player_client={client}"
                                )
                                await status.update(
                                    _status_text(
                                        "🔁", "یوتیوب موقتاً درخواست رو رد کرد، تلاش دوباره...",
                                        percent=0, details=_details,
                                    ),
                                    force=True,
                                )
                                retry_args = ['--extractor-args', f'youtube:player_client={client}']
                                if settings.YOUTUBE_COOKIE_FILE:
                                    retry_args += ['--cookies', settings.YOUTUBE_COOKIE_FILE]
                                retry_cmd = cmd[:1] + retry_args + cmd[1:]
                                returncode, stderr_text = await _run_ytdlp_with_progress(
                                    retry_cmd, status, settings.DOWNLOAD_TIMEOUT, details=_details
                                )
                                if returncode == 0:
                                    break

                        # فال‌بک ۱: ساندکلاود (scsearch) - وقتی یوتیوب به هر دلیلی
                        # (403/چالش ضدربات/بدون نتیجه) جواب نداد، همون جست‌وجو رو
                        # تو ساندکلاود امتحان می‌کنیم - منبع کاملاً متفاوت
                        if returncode != 0:
                            logger.warning(
                                f"job {job_id}: جست‌وجوی یوتیوب برای اسپاتیفای ناموفق بود "
                                f"({(stderr_text or '')[:120]})، تلاش با ساندکلاود..."
                            )
                            await status.update(
                                _status_text(
                                    "🔁", "یوتیوب جواب نداد، امتحان با ساندکلاود...",
                                    percent=0, details=_details,
                                ),
                                force=True,
                            )
                            sc_cmd = [cmd[0], f"scsearch1:{search_query} audio"] + cmd[2:]
                            returncode, stderr_text = await _run_ytdlp_with_progress(
                                sc_cmd, status, settings.DOWNLOAD_TIMEOUT, details=_details
                            )

                        # فال‌بک ۲ (آخر): پیش‌نمایش رسمی ۳۰ ثانیه‌ای خود اسپاتیفای -
                        # mp3 آزاد روی CDN عمومی‌شون؛ صادقانه با پسوند «پیش‌نمایش»
                        # تو عنوان ارسال می‌شه تا کاربر بدونه فایل کامل نیست
                        preview_used = False
                        if returncode != 0 and track.preview_url:
                            logger.warning(
                                f"job {job_id}: ساندکلاود هم ناموفق بود "
                                f"({(stderr_text or '')[:120]})، استفاده از پیش‌نمایش رسمی اسپاتیفای..."
                            )
                            await status.update(
                                _status_text(
                                    "🔁", "فایل کامل در دسترس نبود؛ پیش‌نمایش رسمی می‌فرستم...",
                                    percent=0, details=_details,
                                ),
                                force=True,
                            )
                            try:
                                prev_path = await _download_generic_file(
                                    track.preview_url, temp_dir, status=status, details=_details
                                )
                            except Exception as e:
                                logger.warning(f"دانلود پیش‌نمایش اسپاتیفای برای job {job_id} ناموفق بود: {e}")
                                prev_path = None
                            if prev_path and prev_path.exists() and prev_path.stat().st_size > 0:
                                if prev_path.suffix != ".mp3":
                                    prev_path = prev_path.rename(prev_path.with_suffix(".mp3"))
                                file_path = prev_path
                                preview_used = True
                                download_method = "spotify-preview-30s"
                                await DownloadJobCRUD.update_status(
                                    session, job_id, DownloadStatus.DOWNLOADING,
                                    title=f"{track.title} (پیش‌نمایش ۳۰ ثانیه‌ای)",
                                    artist=track.artist,
                                )

                        if returncode != 0 and not preview_used:
                            raise RuntimeError(
                                "دانلود صدا از یوتیوب و ساندکلاود ناموفق بود: "
                                f"{(stderr_text or '')[:200]}"
                            )

                        if not preview_used:
                            mp3_candidates = sorted(
                                temp_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True
                            )
                            if not mp3_candidates:
                                raise RuntimeError("فایل صوتی بعد از دانلود پیدا نشد")
                            file_path = mp3_candidates[0]

                        try:
                            await tag_audio_file(file_path, track)
                        except Exception as e:
                            logger.warning(f"تگ‌گذاری فایل اسپاتیفای برای job {job_id} ناموفق بود: {e}")

                        if not job.title:
                            await DownloadJobCRUD.update_status(
                                session, job_id, DownloadStatus.DOWNLOADING,
                                title=track.title, artist=track.artist,
                            )
                    except Exception as e:
                        await DownloadJobCRUD.update_status(
                            session, job_id, DownloadStatus.FAILED,
                            error_msg=f"خطا در دانلود از اسپاتیفای: {str(e)[:255]}"
                        )
                        logger.error(f"خطا در دانلود اسپاتیفای برای job {job_id}: {e}")
                        await DownloadStatsCRUD.increment_today(session, successful=False, site=job.detected_site)
                        is_playlist_error = "پلی‌لیست" in str(e) or "آلبوم" in str(e)
                        await _notify_failure(
                            job.chat_id, job_id,
                            str(e) if is_playlist_error else (
                                "نتونستم این آهنگ رو از اسپاتیفای دانلود کنم. ممکنه لینک اشتباه "
                                "باشه یا این آهنگ تو یوتیوب پیدا نشه."
                            ),
                            message_id=job.message_id,
                            tech_detail=str(e),
                        )
                        await _delete_status_message(job)
                        _remove_job_dir(job_id)
                        return None

                    if not file_path or not file_path.exists():
                        await DownloadJobCRUD.update_status(
                            session, job_id, DownloadStatus.FAILED,
                            error_msg="فایلی پیدا نشد"
                        )
                        logger.error(f"فایلی برای job {job_id} بعد از دانلود اسپاتیفای پیدا نشد")
                        await DownloadStatsCRUD.increment_today(session, successful=False, site=job.detected_site)
                        await _notify_failure(
                            job.chat_id, job_id,
                            "دانلود از اسپاتیفای به مشکل خورد و چیزی روی سرور پیدا نشد.",
                            message_id=job.message_id
                        )
                        await _delete_status_message(job)
                        _remove_job_dir(job_id)
                        return None

                else:
                    # تنظیمات yt-dlp بر اساس نوع فایل
                    outtmpl = str(temp_dir / '%(title)s.%(ext)s')

                    # پلی‌لیست/ست ساندکلاود - چک با روی خودِ ساختار URL کافی نیست،
                    # چون لینک‌های کوتاه‌شده‌ی اپ موبایل (on.soundcloud.com/xxxx)
                    # هیچ نشونه‌ای تو خودِ متن لینک ندارن که ترکه یا پلی‌لیست -
                    # فقط بعد از resolve شدن توسط yt-dlp معلوم می‌شه. برای همین به‌جای
                    # پارس کردن URL، همیشه (برای هر لینک ساندکلاودی) اول با
                    # --flat-playlist می‌پرسیم؛ اگه بیش از یه ترک برگردوند، یعنی
                    # واقعاً پلی‌لیست/ست بوده و فن‌اوت می‌کنیم (دقیقاً الگوی پلی‌لیست
                    # اسپاتیفای بالا)؛ اگه فقط یه ترک بود (یا probe fail شد)، ادامه
                    # می‌دیم به همون مسیر دانلود تک‌فایلِ زیر - بدون هیچ تغییری.
                    if job.detected_site == SiteType.SOUNDCLOUD.value:
                        playlist_track_urls = await _probe_soundcloud_playlist_tracks(job.url)
                        if playlist_track_urls and len(playlist_track_urls) > 1:
                            total_tracks = len(playlist_track_urls)
                            playlist_batch_id = str(job_id)
                            spawned = 0
                            for idx, track_url in enumerate(playlist_track_urls, start=1):
                                try:
                                    sub_status_msg = await bot.send_message(
                                        job.chat_id, "⏳ تو صف دانلود...",
                                        reply_to_message_id=job.message_id or None,
                                    )
                                    sub_job = await DownloadJobCRUD.create(
                                        session,
                                        user_id=job.user_id,
                                        chat_id=job.chat_id,
                                        message_id=job.message_id,
                                        status_message_id=sub_status_msg.message_id,
                                        url=track_url,
                                        detected_site=SiteType.SOUNDCLOUD.value,
                                        raw_domain=urlparse(track_url).netloc.lower(),
                                        file_type=FileType.AUDIO,
                                        playlist_id=playlist_batch_id,
                                        playlist_index=idx,
                                        playlist_total=total_tracks,
                                    )
                                    download_file.delay(sub_job.id, "best")
                                    spawned += 1
                                except Exception as e:
                                    logger.warning(
                                        f"ساخت job جدا برای ترک {track_url} "
                                        f"(پلی‌لیست ساندکلاود job {job_id}) ناموفق بود: {e}"
                                    )

                            await DownloadJobCRUD.update_status(session, job_id, DownloadStatus.DONE)
                            if job.status_message_id and spawned > 0:
                                try:
                                    await bot.edit_message_text(
                                        f"☁️ {spawned} تا آهنگ از این پلی‌لیست ساندکلاود پیدا شد، "
                                        f"شروع دانلود...\n{_progress_bar(0)}",
                                        chat_id=job.chat_id, message_id=job.status_message_id,
                                    )
                                except Exception:
                                    pass
                                poll_playlist_progress.apply_async(
                                    args=[job_id, playlist_batch_id], countdown=8
                                )
                            elif job.status_message_id:
                                try:
                                    await bot.edit_message_text(
                                        "❌ هیچ‌کدوم از ترک‌های این پلی‌لیست ساندکلاود قابل صف‌کردن نبودن.",
                                        chat_id=job.chat_id, message_id=job.status_message_id,
                                    )
                                except Exception:
                                    pass
                            _remove_job_dir(job_id)
                            return None
                        # اگه playlist_track_urls خالی/None بود یا فقط یه ترک داشت،
                        # عمداً return نمی‌کنیم - می‌ریم پایین، مسیر عادیِ تک‌فایل

                    # کوکی برای اینستاگرام (پست‌هایی که بدون لاگین قابل مشاهده نیستن)
                    #
                    # نکته‌ی مهم: کوکی لاگین‌شده (sessionid) اینستاگرام رو مجبور می‌کنه
                    # yt-dlp از یه اندپوینت خصوصی دیگه استفاده کنه (که این‌روزها اغلب
                    # ۴۰۴/۴۰۰ می‌ده)، در حالی که همون پست بدون کوکی (مسیر anonymous)
                    # مشکلی نداره. خودِ ریپوی yt-dlp هم این رو به‌عنوان known issue
                    # ثبت کرده: «اگه با کوکی لاگین‌شده fail شد، بدون کوکی امتحان کن».
                    # برای همین حالت اول رو همیشه بدون کوکی می‌زنیم و کوکی رو فقط
                    # به‌عنوان تلاش دوم (وقتی واقعاً پیام login/رِیت‌لیمیت گرفتیم) میاریم.
                    is_instagram_job = job.detected_site in (
                        SiteType.INSTAGRAM.value, SiteType.INSTAGRAM_REEL.value
                    )
                    cookie_args = []
                    instagram_cookie_args = (
                        ['--cookies', settings.INSTAGRAM_COOKIE_FILE]
                        if is_instagram_job and settings.INSTAGRAM_COOKIE_FILE
                        else []
                    )

                    # مقداردهی اولیه بیرون از if/else تا تو مسیر صوتی هم تعریف
                    # شده باشه - قبلاً فقط تو مسیر ویدیو ست می‌شد و اگه دانلود
                    # صوتی با "Requested format is not available" مواجه می‌شد،
                    # چک پایین‌تر (video_format != "best") با NameError کرش می‌کرد
                    video_format = None

                    if job.file_type == FileType.AUDIO:
                        # استخراج فقط صدا و تبدیل به mp3
                        # --embed-thumbnail: تامبنیل خودِ ویدیو (که yt-dlp همینجوری
                        # هم برای هر لینک می‌گیره) رو به‌عنوان کاور/عکس پوشه تو خودِ
                        # فایل mp3 (تگ APIC) جاسازی می‌کنه، تا فایل صوتی تحویلی هم
                        # مثل یه موزیک عادی عکس داشته باشه، نه بدون هیچ کاوری.
                        # --convert-thumbnail jpg چون بعضی سایت‌ها (از جمله خودِ
                        # یوتیوب) تامبنیل رو webp می‌دن که جاسازیش تو ID3 همیشه
                        # پشتیبانی نمی‌شه؛ jpg سازگاری کامل داره.
                        cmd = [
                            'yt-dlp',
                            '-x',
                            '--audio-format', 'mp3',
                            '--audio-quality', '0',
                            '--embed-thumbnail',
                            '--convert-thumbnail', 'jpg',
                            '-o', outtmpl,
                            '--no-playlist',
                            *_impersonate_args(job.url),
                            *cookie_args,
                            job.url
                        ]
                    else:
                        # دانلود ویدیو با کیفیت انتخاب‌شده.
                        #
                        # نکته‌ی مهم: قبلاً فرمت فقط "bestvideo+bestaudio" بود، بدون
                        # هیچ محدودیتی رو کدک. yt-dlp خیلی وقتا "بهترین" استریم رو
                        # VP9 یا AV1 انتخاب می‌کنه (کیفیت/حجم بهتر) و بدون تبدیل توی
                        # container با پسوند mp4 می‌ذارتش. آیفون VP9 رو اصلاً دیکود
                        # نمی‌کنه (و AV1 رو فقط گوشی‌های خیلی جدید) - نتیجه‌ش دقیقاً
                        # همون چیزیه که تو تست دیده شد: صدا پخش می‌شه ولی تصویر نه،
                        # چون AAC قابل‌فهمه ولی ویدیوی VP9/AV1 نه.
                        #
                        # با --format-sort (-S) به‌جای فیلتر []، به yt-dlp می‌گیم
                        # "اگه h264/aac بود اونو ترجیح بده" نه "فقط h264/aac قبول
                        # کن" - یعنی برخلاف فیلتر []، اگه یه سایت اصلاً h264 نداشته
                        # باشه، دانلود fail نمی‌شه؛ فقط با بهترین گزینه‌ی بعدی ادامه
                        # می‌ده. این دقیقاً روش پیشنهادی خودِ مستندات yt-dlp برای
                        # سازگاری حداکثری با پلیرهای موبایل (آیفون/تلگرام/اندروید) هست.
                        format_sort_args = ['-S', 'vcodec:h264,acodec:aac']
                        quality_format_map = {
                            "best": "bv*+ba/b",
                            "1080p": "bv*[height<=1080]+ba/b[height<=1080]",
                            "720p": "bv*[height<=720]+ba/b[height<=720]",
                            "480p": "bv*[height<=480]+ba/b[height<=480]",
                            # پیام ویدیویی به‌هرحال به 384x384 ریسایز می‌شه، پس دانلود با
                            # کیفیت بالاتر لازم نیست و فقط دانلود رو کند می‌کنه
                            "video_note": "bv*[height<=720]+ba/b[height<=720]",
                        }
                        video_format = quality_format_map.get(quality, "bv*+ba/b")
                        cmd = [
                            'yt-dlp',
                            '--format', video_format,
                            *format_sort_args,
                            '--merge-output-format', 'mp4',
                            '-o', outtmpl,
                            '--no-playlist',
                            *_impersonate_args(job.url),
                            *cookie_args,
                            job.url
                        ]

                    returncode, stderr_text = await _run_ytdlp_with_progress(
                        cmd, status, settings.DOWNLOAD_TIMEOUT, details=_details
                    )
                    result = type("_Result", (), {"returncode": returncode, "stderr": stderr_text})()

                    # بعضی پست‌ها (مخصوصاً اینستاگرام) فرمت جدا برای هر کیفیت ندارن
                    # و ترکیب height<=X ارور "Requested format is not available" می‌ده.
                    # یه بار با فرمت "best" (هر چی که خودِ سایت داره) دوباره امتحان می‌کنیم.
                    if (result.returncode != 0
                            and "Requested format is not available" in (result.stderr or "")
                            and video_format is not None
                            and video_format != "bv*+ba/b"):
                        logger.warning(
                            f"فرمت {quality} برای job {job_id} در دسترس نبود، تلاش دوباره با best"
                        )
                        fallback_cmd = [
                            'yt-dlp',
                            '--format', 'bv*+ba/b',
                            '-S', 'vcodec:h264,acodec:aac',
                            '--merge-output-format', 'mp4',
                            '-o', outtmpl,
                            '--no-playlist',
                            *_impersonate_args(job.url),
                            *cookie_args,
                            job.url
                        ]
                        await status.update(
                            _status_text(
                                "🔁", "کیفیت انتخابی در دسترس نبود، تلاش با بهترین کیفیت موجود...",
                                percent=0, details=_details,
                            ),
                            force=True,
                        )
                        returncode, stderr_text = await _run_ytdlp_with_progress(
                            fallback_cmd, status, settings.DOWNLOAD_TIMEOUT, details=_details
                        )
                        result = type("_Result", (), {"returncode": returncode, "stderr": stderr_text})()

                    # اگه بدون کوکی fail شد و علامت‌های نیاز به لاگین/رِیت‌لیمیت رو
                    # داشت (login_required، ۴۰۰/۴۰۱/۴۰۴ روی video info)، یه بار دیگه
                    # با کوکی امتحان می‌کنیم - این پستی که واقعاً private/سن‌محدوده رو
                    # می‌گیره، بدون اینکه پست‌های عمومی رو با کوکی خراب‌شده fail کنه.
                    if (
                        result.returncode != 0
                        and is_instagram_job
                        and instagram_cookie_args
                        and not cookie_args
                        and any(
                            marker in (result.stderr or "")
                            for marker in (
                                "login_required", "login required", "rate-limit",
                                "HTTP Error 400", "HTTP Error 401", "HTTP Error 404",
                            )
                        )
                    ):
                        logger.warning(
                            f"job {job_id}: yt-dlp بدون کوکی fail شد، تلاش دوباره با کوکی اینستاگرام"
                        )
                        await status.update(
                            _status_text(
                                "🔁", "تلاش دوباره با اکانت لاگین‌شده...",
                                percent=0, details=_details,
                            ),
                            force=True,
                        )
                        # cmd قبلاً بدون کوکی ساخته شده و job.url همیشه آخرین آیتمشه؛
                        # کوکی رو درست قبل از URL تزریق می‌کنیم بدون بازسازی کل cmd.
                        cookie_args = instagram_cookie_args
                        retry_cmd = cmd[:-1] + [*cookie_args, cmd[-1]]
                        returncode, stderr_text = await _run_ytdlp_with_progress(
                            retry_cmd, status, settings.DOWNLOAD_TIMEOUT, details=_details
                        )
                        result = type("_Result", (), {"returncode": returncode, "stderr": stderr_text})()

                    # یوتیوب این‌روزها گاهی IP سرورهای دیتاسنتری رو با 403 رد
                    # می‌کنه (حتی برای ویدیوهای کاملاً عمومی)، چون player_client
                    # پیش‌فرض («web») این IPها رو مشکوک تشخیص می‌ده. اگه به این
                    # خطا خوردیم و لینک واقعاً یوتیوبه (youtube.com/youtu.be/
                    # music.youtube.com)، به‌ترتیب با player_client=mweb،
                    # player_client=android و در آخر player_client=tv دوباره
                    # امتحان می‌کنیم؛ اگه کوکی اختیاری (YOUTUBE_COOKIE_FILE)
                    # هم ست شده باشه، به هر تلاش اضافه می‌شه.
                    if (
                        result.returncode != 0
                        and _is_youtube_url(job.url)
                        and "403" in (result.stderr or "")
                    ):
                        # "tv" رو به‌عنوان تلاش سوم اضافه کردیم: این کلاینت برای
                        # خیلی از ویدیوهای عمومی حتی بدون کوکی/لاگین گیت بات-چک
                        # نمی‌خوره (چون یوتیوب رفتار دستگاه‌های تلویزیونی رو با
                        # سخت‌گیری کمتری بررسی می‌کنه)، برخلاف mweb/android که
                        # این‌روزها گاهی خودشون هم به همون 403 می‌خورن.
                        for client in ("mweb", "android", "tv"):
                            logger.warning(
                                f"job {job_id}: یوتیوب با 403 جواب داد، تلاش دوباره با "
                                f"player_client={client}"
                            )
                            await status.update(
                                _status_text(
                                    "🔁", "یوتیوب موقتاً درخواست رو رد کرد، تلاش دوباره...",
                                    percent=0, details=_details,
                                ),
                                force=True,
                            )
                            youtube_retry_args = ['--extractor-args', f'youtube:player_client={client}']
                            if settings.YOUTUBE_COOKIE_FILE:
                                youtube_retry_args += ['--cookies', settings.YOUTUBE_COOKIE_FILE]
                            retry_cmd = cmd[:-1] + youtube_retry_args + [cmd[-1]]
                            returncode, stderr_text = await _run_ytdlp_with_progress(
                                retry_cmd, status, settings.DOWNLOAD_TIMEOUT, details=_details
                            )
                            result = type("_Result", (), {"returncode": returncode, "stderr": stderr_text})()
                            if result.returncode == 0:
                                break

                    if result.returncode != 0:
                        tiktok_fallback_ok = False
                        tiktok_fallback_detail = None
                        if job.detected_site == SiteType.TIKTOK.value:
                            # اکسترکتور وب یت-دی‌ال‌پی برای تیک‌تاک این‌روزها به‌خاطر
                            # تغییرات مکرر مکانیزم challenge/anti-bot خودِ تیک‌تاک
                            # زیاد fail می‌شه (خطای "Unexpected response from webpage
                            # request") - این یه مشکل شناخته‌شده‌ی سمت خودِ یت-دی‌ال‌پیه
                            # که تیمشون معمولاً طی چند روز پچ می‌کنن، نه چیزی که با
                            # کوکی/نسخه/سرور ما درست بشه. قبل از اعلام خطا، از یه
                            # API واسط عمومی (tikwm.com) که مستقیم از API اپ موبایل
                            # می‌خونه (نه صفحه‌ی وب) یه بار دیگه امتحان می‌کنیم.
                            try:
                                from utils.tiktok_downloader import fetch_tiktok_video

                                logger.warning(
                                    f"yt-dlp برای job {job_id} (تیک‌تاک) fail شد "
                                    f"({(result.stderr or '')[:200]})، تلاش با tikwm.com..."
                                )
                                await status.update(
                                    _status_text(
                                        "🔁", "تلاش دوباره با یه روش دیگه...",
                                        percent=0, details=_details,
                                    ),
                                    force=True,
                                )
                                tt_media = await fetch_tiktok_video(job.url)
                                media_url = None
                                if tt_media and job.file_type == FileType.AUDIO:
                                    media_url = tt_media.audio_url or tt_media.video_url
                                elif tt_media:
                                    media_url = tt_media.video_url

                                if media_url:
                                    fb_path = await _download_generic_file(
                                        media_url, temp_dir, status=status, details=_details
                                    )
                                    if fb_path and not fb_path.suffix:
                                        ext = ".mp3" if job.file_type == FileType.AUDIO else ".mp4"
                                        fb_path = fb_path.rename(fb_path.with_suffix(ext))
                                    if fb_path and fb_path.exists():
                                        tiktok_fallback_ok = True
                                        download_method = "tiktok-tikwm-fallback"
                                        if tt_media.title and not job.title:
                                            await DownloadJobCRUD.update_status(
                                                session, job_id, DownloadStatus.DOWNLOADING,
                                                title=tt_media.title,
                                            )
                                    else:
                                        tiktok_fallback_detail = "دانلود فایل از لینک tikwm.com شکست خورد"
                                elif tt_media and tt_media.is_image_post:
                                    tiktok_fallback_detail = (
                                        "این پست یه اسلایدشوی عکسیه، فعلاً برای این نوع پست از "
                                        "tikwm.com پشتیبانی نداریم"
                                    )
                                else:
                                    tiktok_fallback_detail = (
                                        "tikwm.com هم لینکی برنگردوند (احتمالاً پست حذف‌شده/"
                                        "خصوصیه یا خودِ tikwm.com موقتاً از کار افتاده)"
                                    )
                            except Exception as e:
                                tiktok_fallback_detail = str(e)[:200]
                                logger.warning(
                                    f"فال‌بک tikwm.com هم برای job {job_id} ناموفق بود: {e}"
                                )
                                tiktok_fallback_ok = False

                            if not tiktok_fallback_ok:
                                logger.error(
                                    f"هر دو روش دانلود تیک‌تاک برای job {job_id} fail شدن - "
                                    f"yt-dlp: {(result.stderr or '')[:200]} | "
                                    f"tikwm.com: {tiktok_fallback_detail}"
                                )

                        instagram_fallback_ok = False
                        instagram_fallback_detail = None
                        if (
                            not tiktok_fallback_ok
                            and job.detected_site in (SiteType.INSTAGRAM.value, SiteType.INSTAGRAM_REEL.value)
                            and job.file_type != FileType.AUDIO
                        ):
                            # اکسترکتور ویدیوی yt-dlp برای اینستاگرام این‌روزها زیاد با
                            # "HTTP Error 400" (کوکی منقضی/rate limit) fail می‌شه. قبل
                            # از اعلام خطا، با همون API داخلی اینستاگرام (که برای
                            # پست‌های عکسی هم استفاده می‌کنیم) یه بار دیگه امتحان می‌کنیم.
                            try:
                                from utils.instagram_downloader import fetch_instagram_post

                                logger.warning(
                                    f"yt-dlp برای job {job_id} fail شد ({(result.stderr or '')[:200]})، "
                                    f"تلاش با API داخلی اینستاگرام..."
                                )
                                await status.update(
                                    _status_text(
                                        "🔁", "تلاش دوباره با یه روش دیگه...",
                                        percent=0, details=_details,
                                    ),
                                    force=True,
                                )
                                ig_post = await fetch_instagram_post(
                                    job.url, cookie_file=settings.INSTAGRAM_COOKIE_FILE
                                )
                                if ig_post and ig_post.items:
                                    main_item = ig_post.items[0]
                                    fb_path = await _download_generic_file(
                                        main_item.url, temp_dir, status=status, details=_details
                                    )
                                    if fb_path and not fb_path.suffix:
                                        ext = ".mp4" if main_item.type == "video" else ".jpg"
                                        fb_path = fb_path.rename(fb_path.with_suffix(ext))
                                    if fb_path and fb_path.exists():
                                        instagram_fallback_ok = True
                                        download_method = f"instagram-api-fallback ({main_item.type})"
                                    else:
                                        instagram_fallback_detail = "دانلود فایل از لینک API داخلی شکست خورد"
                                else:
                                    # fetch_instagram_post خودش استثنا پرت نکرد ولی چیزی هم
                                    # برنگردوند - یعنی پست پیدا نشد یا private/حذف‌شده‌ست یا
                                    # کوکی دیگه واسه‌ی این محتوا کافی نیست
                                    instagram_fallback_detail = (
                                        "API داخلی هم پستی برنگردوند (احتمالاً پست خصوصی/حذف‌شده "
                                        "یا کوکی دیگه کافی نیست)"
                                    )
                            except Exception as e:
                                instagram_fallback_detail = str(e)[:200]
                                logger.warning(
                                    f"فال‌بک API داخلی اینستاگرام هم برای job {job_id} ناموفق بود: {e}"
                                )
                                instagram_fallback_ok = False

                            if not instagram_fallback_ok:
                                logger.error(
                                    f"هر دو روش دانلود اینستاگرام برای job {job_id} fail شدن - "
                                    f"yt-dlp: {(result.stderr or '')[:200]} | "
                                    f"API داخلی: {instagram_fallback_detail}"
                                )

                        # ساندکلاود: بعضی ترک‌ها (معمولاً Go+/پریمیوم) با HLS
                        # رمزنگاری‌شده سرو می‌شن و yt-dlp درست تشخیص می‌ده که
                        # DRM داره - این قابل‌دور زدن نیست و نباید باشه. ولی به‌جای
                        # همون‌جا شکست اعلام کردن، دقیقاً مثل مسیر اسپاتیفای بالا،
                        # اسم/خواننده‌ی ترک رو از متادیتای خودِ ساندکلاود می‌گیریم و
                        # همون آهنگ رو (اگه پیدا بشه) از یوتیوب دانلود می‌کنیم - یه
                        # منبع کاملاً متفاوت و بدون DRM، نه دور زدن حفاظت خودِ ساندکلاود.
                        soundcloud_fallback_ok = False
                        soundcloud_fallback_detail = None
                        if (
                            not tiktok_fallback_ok
                            and not instagram_fallback_ok
                            and job.detected_site == SiteType.SOUNDCLOUD.value
                            and "drm" in (result.stderr or "").lower()
                        ):
                            try:
                                logger.warning(
                                    f"yt-dlp برای job {job_id} (ساندکلاود) DRM داد، "
                                    f"تلاش جست‌وجو رو یوتیوب..."
                                )
                                await status.update(
                                    _status_text(
                                        "🔁", "این ترک محافظت‌شده‌ست، تلاش با یه منبع دیگه...",
                                        percent=0, details=_details,
                                    ),
                                    force=True,
                                )
                                title_cmd = [
                                    'yt-dlp', '--print', '%(title)s %(uploader)s',
                                    '--skip-download', job.url,
                                ]
                                title_proc = await asyncio.create_subprocess_exec(
                                    *title_cmd,
                                    stdout=asyncio.subprocess.PIPE,
                                    stderr=asyncio.subprocess.PIPE,
                                )
                                title_stdout, _ = await asyncio.wait_for(
                                    title_proc.communicate(), timeout=30
                                )
                                search_query = (
                                    title_stdout.decode(errors="ignore").strip().splitlines()[0]
                                    if title_stdout else None
                                )

                                if search_query:
                                    outtmpl_sc = str(temp_dir / '%(title)s.%(ext)s')
                                    sc_cmd = [
                                        'yt-dlp', f'ytsearch1:{search_query} audio',
                                        '-x', '--audio-format', 'mp3', '--audio-quality', '0',
                                        '-o', outtmpl_sc, '--no-playlist',
                                    ]
                                    sc_returncode, sc_stderr = await _run_ytdlp_with_progress(
                                        sc_cmd, status, settings.DOWNLOAD_TIMEOUT, details=_details
                                    )
                                    if sc_returncode == 0:
                                        mp3_candidates = sorted(
                                            temp_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True
                                        )
                                        if mp3_candidates:
                                            soundcloud_fallback_ok = True
                                            download_method = "soundcloud-youtube-fallback"
                                            if not job.title:
                                                await DownloadJobCRUD.update_status(
                                                    session, job_id, DownloadStatus.DOWNLOADING,
                                                    title=search_query,
                                                )
                                        else:
                                            soundcloud_fallback_detail = "دانلود از یوتیوب تموم شد ولی فایلی پیدا نشد"
                                    else:
                                        soundcloud_fallback_detail = (
                                            f"جست‌وجوی یوتیوب هم fail شد: {(sc_stderr or '')[:150]}"
                                        )
                                else:
                                    soundcloud_fallback_detail = "نتونستم اسم ترک رو از ساندکلاود برای جست‌وجو بگیرم"
                            except Exception as e:
                                soundcloud_fallback_detail = str(e)[:200]
                                logger.warning(
                                    f"فال‌بک یوتیوب برای ساندکلاود job {job_id} ناموفق بود: {e}"
                                )
                                soundcloud_fallback_ok = False

                            if not soundcloud_fallback_ok:
                                logger.error(
                                    f"فال‌بک یوتیوب برای ترک DRM-دار ساندکلاود (job {job_id}) هم ناموفق بود: "
                                    f"{soundcloud_fallback_detail}"
                                )

                        if not instagram_fallback_ok and not tiktok_fallback_ok and not soundcloud_fallback_ok:
                            error_msg = result.stderr or "خطا نامشخص"
                            if instagram_fallback_detail:
                                error_msg = (
                                    f"yt-dlp: {error_msg[:150]}\n"
                                    f"API داخلی اینستاگرام: {instagram_fallback_detail}"
                                )
                            elif tiktok_fallback_detail:
                                error_msg = (
                                    f"yt-dlp: {error_msg[:150]}\n"
                                    f"tikwm.com: {tiktok_fallback_detail}"
                                )
                            elif soundcloud_fallback_detail:
                                error_msg = (
                                    f"yt-dlp: {error_msg[:150]}\n"
                                    f"فال‌بک یوتیوب: {soundcloud_fallback_detail}"
                                )
                            await DownloadJobCRUD.update_status(
                                session, job_id, DownloadStatus.FAILED,
                                error_msg=error_msg[:255]
                            )
                            logger.error(f"خطا در دانلود: {error_msg}")
                            await DownloadStatsCRUD.increment_today(session, successful=False, site=job.detected_site)
                            _reason = _classify_download_error(error_msg) or (
                                "دانلود این لینک با خطا مواجه شد. ممکنه لینک منقضی شده، محدود به کشور خاصی باشه یا از این سایت پشتیبانی نشه."
                            )
                            await _notify_failure(
                                job.chat_id, job_id,
                                _reason,
                                message_id=job.message_id,
                                tech_detail=error_msg,
                            )
                            await _delete_status_message(job)
                            _remove_job_dir(job_id)
                            return None

                    # یافتن فایل دانلود شده
                    files = list(temp_dir.glob('*'))
                    if not files:
                        await DownloadJobCRUD.update_status(
                            session, job_id, DownloadStatus.FAILED,
                            error_msg="فایلی پیدا نشد"
                        )
                        logger.error(f"دانلود job {job_id} با yt-dlp تموم شد ولی فایلی توی پوشه مؤقت پیدا نشد")
                        await DownloadStatsCRUD.increment_today(session, successful=False, site=job.detected_site)
                        await _notify_failure(
                            job.chat_id, job_id,
                            "دانلود تموم شد ولی فایلی پیدا نشد. لطفاً دوباره امتحان کن.",
                            message_id=job.message_id
                        )
                        await _delete_status_message(job)
                        _remove_job_dir(job_id)
                        return None

                    file_path = files[0]

                if job.file_type == FileType.VIDEO_NOTE:
                    await status.update(
                        _status_text("🎞", "در حال آماده‌سازی پیام ویدیویی (گرد)...", percent=100, details=_details),
                        force=True,
                    )
                    note_path = _prepare_video_note(file_path, temp_dir)
                    if not note_path:
                        await DownloadJobCRUD.update_status(
                            session, job_id, DownloadStatus.FAILED,
                            error_msg="خطا در ساخت پیام ویدیویی"
                        )
                        logger.error(f"ساخت پیام ویدیویی برای job {job_id} ناموفق بود")
                        await DownloadStatsCRUD.increment_today(session, successful=False, site=job.detected_site)
                        await _notify_failure(
                            job.chat_id, job_id,
                            "تبدیل ویدیو به پیام ویدیویی (گرد) ناموفق بود. لطفاً دوباره امتحان کن یا کیفیت دیگه‌ای رو انتخاب کن.",
                            message_id=job.message_id
                        )
                        await _delete_status_message(job)
                        _remove_job_dir(job_id)
                        return None
                    file_path = note_path

                file_size_mb = file_path.stat().st_size / (1024 * 1024)
                
                # بررسی سایز
                max_size = await BotConfigCRUD.get_int(
                    session, "max_file_size_mb", settings.MAX_FILE_SIZE_MB
                )
                if file_size_mb > max_size:
                    await DownloadJobCRUD.update_status(
                        session, job_id, DownloadStatus.FAILED,
                        error_msg=f"فایل بیش از حد بزرگ است ({file_size_mb:.2f} MB)"
                    )
                    logger.warning(f"job {job_id} رد شد: حجم {file_size_mb:.2f}MB بیشتر از سقف {max_size}MB")
                    await DownloadStatsCRUD.increment_today(session, successful=False, size_mb=file_size_mb, site=job.detected_site)
                    await _notify_failure(
                        job.chat_id, job_id,
                        f"📦 حجم فایل <b>{file_size_mb:.1f} مگابایت</b> شده که بیشتر از سقف مجاز "
                        f"(<b>{max_size} مگابایت</b>) هست.\nمی‌تونی کیفیت پایین‌تری رو امتحان کنی.",
                        message_id=job.message_id
                    )
                    await _delete_status_message(job)
                    _remove_job_dir(job_id)
                    return None
                
                # بروزرسانی کار
                await DownloadJobCRUD.update_status(
                    session, job_id, DownloadStatus.UPLOADING,
                    file_name=file_path.name,
                    file_size_mb=file_size_mb
                )
                await status.update(
                    _status_text(
                        "📤", "در حال آپلود به تلگرام...", percent=100,
                        note=f"💾 حجم فایل: {file_size_mb:.1f} مگابایت",
                        details=_details,
                    ),
                    force=True,
                )

                if job.file_type == FileType.VIDEO or (job.file_type == FileType.DOCUMENT and _is_video_file(file_path)):
                    # تامبنیل بسازیم تا تلگرام صفحه‌ی سیاه نشون نده
                    _generate_thumbnail(file_path, temp_dir)

                logger.info(
                    f"دانلود موفق: {file_path.name} ({file_size_mb:.2f} MB) - "
                    f"روش: {download_method} - job {job_id}"
                )

                # اگه کاربر علاوه بر آپلود تلگرام لینک دانلود مستقیم هم خواسته بود،
                # همین‌جا (قبل از شروع آپلود تلگرام که ممکنه طولانی بشه) لینکش رو
                # می‌سازیم و جدا می‌فرستیم - این‌جوری برای فایل‌های حجیم، کاربر مجبور
                # نیست منتظر تموم‌شدن آپلود تلگرام بمونه تا لینک مستقیمش رو بگیره.
                if getattr(job, "want_direct_link", False):
                    from utils.direct_links import create_direct_link

                    link_result = create_direct_link(
                        job_id, file_path, custom_filename=getattr(job, "direct_link_filename", None)
                    )
                    if link_result:
                        token, url = link_result
                        expires_at = datetime.utcnow() + timedelta(seconds=settings.DIRECT_LINK_EXPIRE_SECONDS)
                        await DownloadJobCRUD.set_direct_link(session, job_id, token, url, expires_at)
                        await _send_direct_link_message(job_id, job.chat_id, job.message_id or None, token, url)
                        cleanup_direct_link.apply_async(
                            args=[token], countdown=settings.DIRECT_LINK_EXPIRE_SECONDS
                        )
                    else:
                        try:
                            await bot.send_message(
                                job.chat_id,
                                "⚠️ ساخت لینک دانلود مستقیم ممکن نشد؛ فایل فقط از طریق تلگرام ارسال می‌شه.",
                                parse_mode="HTML",
                                reply_to_message_id=job.message_id or None,
                            )
                        except Exception as e:
                            logger.warning(f"ارسال پیام خطای لینک مستقیم برای job {job_id} ناموفق بود: {e}")

                # ارسال به صف آپلود
                send_to_telegram.delay(job_id, str(file_path))

                return str(file_path)
            
            except Exception as e:
                logger.error(f"خطا در دانلود: {e}", exc_info=True)
                await DownloadJobCRUD.update_status(
                    session, job_id, DownloadStatus.FAILED,
                    error_msg=str(e)[:255]
                )
                await DownloadStatsCRUD.increment_today(session, successful=False, site=job.detected_site)
                _reason = _classify_download_error(e) or "یه خطای غیرمنتظره پیش اومد و دانلود انجام نشد."
                await _notify_failure(
                    job.chat_id, job_id,
                    _reason,
                    message_id=job.message_id,
                    tech_detail=str(e),
                )
                await _delete_status_message(job)
                _remove_job_dir(job_id)
                return None
    
    try:
        return run_async(_download())
    finally:
        concurrency.release(job_id)


@celery_app.task(bind=True, queue='download')
def download_uploaded_file(self, job_id: int, file_id: str):
    """
    مسیر جدید: کاربر خودش مستقیم یه فایل/ویدیو/... به بات فرستاده (نه لینک).
    برخلاف download_file، اینجا نیازی به yt-dlp/دانلود از سایت خارجی نیست -
    فایل از قبل تو تلگراف هست، فقط باید از سرورهای تلگرام (یا مستقیم از
    دیسک، اگه Local Bot API Server باشه) بگیریمش رو دیسک خودمون تا بتونیم
    براش لینک دانلود مستقیم بسازیم. بعدش (چون از قبل تو تلگرام هست) نیازی
    به آپلود دوباره نیست - فقط لینک رو می‌فرستیم.
    """
    logger.info(f"شروع دانلود فایلِ آپلودیِ کاربر برای job {job_id}")

    limit = settings.MAX_CONCURRENT_DOWNLOADS
    poll_interval = 3
    max_wait = settings.DOWNLOAD_TIMEOUT
    waited = 0
    while not concurrency.try_acquire(job_id, limit):
        if waited == 0:
            logger.info(f"ظرفیت دانلود همزمان پره (سقف {limit})، job {job_id} منتظر موند")
        if waited >= max_wait:
            run_async(_notify_capacity_timeout(job_id))
            return None
        time.sleep(poll_interval)
        waited += poll_interval

    async def _process():
        async with async_session_maker() as session:
            job = await DownloadJobCRUD.get_by_id(session, job_id)
            if not job:
                return None

            if job.status == DownloadStatus.CANCELLED:
                logger.info(f"job {job_id} قبل از شروع لغو شده بود، رد شد")
                return None

            await DownloadJobCRUD.update_status(session, job_id, DownloadStatus.DOWNLOADING)

            status = _StatusUpdater(job)
            _details = "📥 منبع: فایل ارسالی خود کاربر"
            await status.update(
                _status_text("⬇️", "در حال دریافت فایل از تلگرام", percent=0, details=_details),
                force=True,
            )

            temp_dir = Path(settings.TEMP_DIR) / str(job_id)
            temp_dir.mkdir(parents=True, exist_ok=True)

            try:
                # اسم فایل موقتی رو از عنوان ذخیره‌شده (که تو بات از فایل اصلی
                # گرفته شده) می‌سازیم؛ اگه نبود، اسم عمومی می‌ذاریم.
                raw_name = job.title or f"upload_{job_id}"
                safe_name = safe_upload_filename(raw_name) or f"upload_{job_id}"
                dest_path = temp_dir / safe_name
                _expected_total_bytes = (job.file_size_mb * 1024 * 1024) if job.file_size_mb else None

                try:
                    await _download_with_progress(file_id, dest_path, _expected_total_bytes, status, _details)
                except Exception as e:
                    logger.error(f"دریافت فایل آپلودی از تلگرام برای job {job_id} ناموفق بود: {e}")
                    await DownloadJobCRUD.update_status(
                        session, job_id, DownloadStatus.FAILED,
                        error_msg=str(e)[:255]
                    )
                    await DownloadStatsCRUD.increment_today(session, successful=False, site=job.detected_site)
                    await _notify_failure(
                        job.chat_id, job_id,
                        "دریافت فایل از تلگرام ناموفق بود. لطفاً دوباره امتحان کن.",
                        message_id=job.message_id,
                        tech_detail=str(e),
                    )
                    await _delete_status_message(job)
                    _remove_job_dir(job_id)
                    return None

                if not dest_path.exists() or dest_path.stat().st_size == 0:
                    await DownloadJobCRUD.update_status(
                        session, job_id, DownloadStatus.FAILED,
                        error_msg="فایلی دریافت نشد"
                    )
                    await DownloadStatsCRUD.increment_today(session, successful=False, site=job.detected_site)
                    await _notify_failure(
                        job.chat_id, job_id,
                        "فایل دریافت نشد. لطفاً دوباره امتحان کن.",
                        message_id=job.message_id,
                    )
                    await _delete_status_message(job)
                    _remove_job_dir(job_id)
                    return None

                file_size_mb = dest_path.stat().st_size / (1024 * 1024)

                max_size = await BotConfigCRUD.get_int(
                    session, "max_file_size_mb", settings.MAX_FILE_SIZE_MB
                )
                if file_size_mb > max_size:
                    await DownloadJobCRUD.update_status(
                        session, job_id, DownloadStatus.FAILED,
                        error_msg=f"فایل بیش از حد بزرگ است ({file_size_mb:.2f} MB)"
                    )
                    await DownloadStatsCRUD.increment_today(session, successful=False, size_mb=file_size_mb, site=job.detected_site)
                    await _notify_failure(
                        job.chat_id, job_id,
                        f"📦 حجم فایل <b>{file_size_mb:.1f} مگابایت</b> شده که بیشتر از سقف مجاز "
                        f"(<b>{max_size} مگابایت</b>) هست.",
                        message_id=job.message_id,
                    )
                    await _delete_status_message(job)
                    _remove_job_dir(job_id)
                    return None

                await status.update(
                    _status_text(
                        "🔗", "در حال ساخت لینک دانلود مستقیم...", percent=100,
                        note=f"💾 حجم فایل: {file_size_mb:.1f} مگابایت",
                        details=_details,
                    ),
                    force=True,
                )

                from utils.direct_links import create_direct_link

                link_result = create_direct_link(
                    job_id, dest_path, custom_filename=getattr(job, "direct_link_filename", None)
                )

                if not link_result:
                    await DownloadJobCRUD.update_status(
                        session, job_id, DownloadStatus.FAILED,
                        error_msg="ساخت لینک دانلود مستقیم ناموفق بود",
                        file_name=dest_path.name, file_size_mb=file_size_mb,
                    )
                    await DownloadStatsCRUD.increment_today(session, successful=False, size_mb=file_size_mb, site=job.detected_site)
                    await _notify_failure(
                        job.chat_id, job_id,
                        "⚠️ ساخت لینک دانلود مستقیم ممکن نشد. لطفاً دوباره امتحان کن.",
                        message_id=job.message_id,
                    )
                    await _delete_status_message(job)
                    _remove_job_dir(job_id)
                    return None

                token, url = link_result
                expires_at = datetime.utcnow() + timedelta(seconds=settings.DIRECT_LINK_EXPIRE_SECONDS)
                await DownloadJobCRUD.set_direct_link(session, job_id, token, url, expires_at)

                await DownloadJobCRUD.update_status(
                    session, job_id, DownloadStatus.DONE,
                    file_name=dest_path.name, file_size_mb=file_size_mb,
                )
                await DownloadStatsCRUD.increment_today(session, successful=True, size_mb=file_size_mb, site=job.detected_site)

                await _send_direct_link_message(job_id, job.chat_id, job.message_id or None, token, url)

                cleanup_direct_link.apply_async(
                    args=[token], countdown=settings.DIRECT_LINK_EXPIRE_SECONDS
                )

                await _delete_status_message(job)
                # فایل موقتِ محلی رو بلافاصله پاک می‌کنیم - نسخه‌ی قابل‌سرو
                # از قبل با هاردلینک/کپی زیر DIRECT_LINK_DIR ساخته شده، پس
                # این پوشه‌ی موقت (temp_dir) دیگه لازم نیست و نباید دیسک رو
                # اشغال نگه داره (برخلاف مسیر معمولی که فایل تا آپلودِ
                # موفق به تلگرام لازمه).
                _remove_job_dir(job_id)
                return str(dest_path)

            except Exception as e:
                logger.error(f"خطا در پردازش فایل آپلودی: {e}", exc_info=True)
                await DownloadJobCRUD.update_status(
                    session, job_id, DownloadStatus.FAILED,
                    error_msg=str(e)[:255]
                )
                await DownloadStatsCRUD.increment_today(session, successful=False, site=job.detected_site)
                await _notify_failure(
                    job.chat_id, job_id,
                    "یه خطای غیرمنتظره پیش اومد و لینک ساخته نشد.",
                    message_id=job.message_id,
                    tech_detail=str(e),
                )
                await _delete_status_message(job)
                _remove_job_dir(job_id)
                return None

    try:
        return run_async(_process())
    finally:
        concurrency.release(job_id)


def _is_image_file(file_path: str) -> bool:
    """آیا فایل دانلود‌شده یه عکسه (مثلاً یه پین عکسی از Pinterest)، نه ویدیو"""
    image_ext = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp')
    return str(file_path).lower().endswith(image_ext)


def _is_video_file(file_path) -> bool:
    """
    آیا فایلِ دانلودشده (از یه لینک مستقیم/cloud-host مثل dropbox/gofile/
    pixeldrain/filelu که با FileType.DOCUMENT دانلود می‌شن) واقعاً یه ویدیوئه.
    اگه آره، به‌جای send_document باید با send_video بره تا تو تلگرام پلیر
    این‌لاین/thumbnail داشته باشه، نه یه کارت دانلود خام.
    """
    video_ext = ('.mp4', '.mkv', '.mov', '.webm', '.avi', '.m4v', '.3gp', '.ts')
    return str(file_path).lower().endswith(video_ext)


@celery_app.task(bind=True, queue='upload')
def send_to_telegram(self, job_id: int, file_path: str):
    """ارسال فایل به تلگرام"""
    logger.info(f"ارسال فایل برای job {job_id}")
    
    async def _send():
        async with async_session_maker() as session:
            job = await DownloadJobCRUD.get_by_id(session, job_id)
            if not job or not os.path.exists(file_path):
                logger.error(f"فایل نیافت: {file_path}")
                if job:
                    await DownloadJobCRUD.update_status(
                        session, job_id, DownloadStatus.FAILED,
                        error_msg="فایل روی سرور پیدا نشد"
                    )
                    await DownloadStatsCRUD.increment_today(session, successful=False, site=job.detected_site)
                    await _notify_failure(
                        job.chat_id, job_id,
                        "فایل دانلود شده روی سرور پیدا نشد و ارسال نشد.",
                        message_id=job.message_id
                    )
                    await _delete_status_message(job)
                return None

            if job.status == DownloadStatus.CANCELLED:
                logger.info(f"job {job_id} لغو شده بود، ارسال نشد")
                await _delete_status_message(job)
                _remove_job_dir(job_id)
                return None

            # تنظیمات شخصی کاربر (منوی /settings): افکت اسپویلر، آپلود اجباری
            # به‌صورت سند، و thumbnail دلخواه. اگه کاربر پیدا نشد (نباید پیش
            # بیاد، ولی محکم‌کاری) هر سه رو خاموش/خالی فرض می‌کنیم.
            user_spoiler = False
            user_force_document = False
            user_thumb_file_id = None
            user_receive_screenshots = True  # پیش‌فرض روشن (None تو دیتابیس هم یعنی روشن)
            try:
                _settings_user = await UserCRUD.get_by_pk(session, job.user_id)
                if _settings_user:
                    user_spoiler = bool(_settings_user.spoiler_effect)
                    user_force_document = bool(_settings_user.upload_as_document)
                    user_thumb_file_id = _settings_user.custom_thumbnail_file_id
                    if _settings_user.receive_screenshots is not None:
                        user_receive_screenshots = bool(_settings_user.receive_screenshots)
            except Exception as e:
                logger.warning(f"خوندن تنظیمات کاربر برای job {job_id} ناموفق بود: {e}")

            try:
                # نوار وضعیت این تسک - قبلاً send_to_telegram هیچ _StatusUpdater
                # نمی‌ساخت، برای همین پیام «در حال آپلود...» که تو download_file
                # فرستاده می‌شد تا آخر کار ثابت می‌موند. همین status_message_id
                # رو این‌جا هم استفاده می‌کنیم تا آپدیت‌های آپلود روی همون پیام برن.
                status = _StatusUpdater(job)
                _details = (
                    f"🌐 سایت: {html.escape(job.detected_site or '-')}\n"
                    f"📦 حجم: {(job.file_size_mb or 0):.1f} مگابایت"
                )

                async def _report_upload_progress(percent: float):
                    await status.update(
                        _status_text(
                            "📤", "در حال آپلود به تلگرام...", percent=percent,
                            details=_details,
                        )
                    )

                # اگه کاربر از پرامپت «تغییر نام فایل» یه اسم دلخواه فرستاده باشه
                # (job.direct_link_filename - همون فیلدی که برای لینک مستقیم هم
                # استفاده می‌شه، صرف‌نظر از این‌که لینک مستقیم خواسته باشه یا نه)،
                # اسم نمایشیِ فایل تو تلگرام رو با همون می‌سازیم؛ پسوند واقعی
                # فایل رو حفظ می‌کنیم تا تلگرام نوع فایل رو درست تشخیص بده.
                custom_upload_filename = None
                if getattr(job, "direct_link_filename", None):
                    real_ext = Path(file_path).suffix
                    custom_upload_filename = f"{job.direct_link_filename}{real_ext}"

                file_obj = _ProgressFSInputFile(
                    file_path, on_progress=_report_upload_progress, filename=custom_upload_filename
                )

                # ارسال بر اساس نوع فایل
                caption = None
                # عنوان/کپشن رو escape می‌کنیم چون خام از سایت میاد و ممکنه کاراکترهای
                # HTML (< > &) داشته باشه که با parse_mode="HTML" فرمت رو خراب می‌کنه.
                # هدر بولد رو کوتاه نگه می‌داریم (سایت‌هایی مثل تیک‌تاک/پینترست به‌جای
                # عنوان واقعی، کل کپشن رو تو همین فیلد می‌فرستن) و بدنه‌ی کپشن (توضیحات
                # کامل پست) رو جدا از بلوک متادیتا (هنرمند/مدت/سایت) می‌سازیم
                safe_title, caption_body = _build_caption_header_and_body(job)

                is_image = job.file_type == FileType.PHOTO or _is_image_file(file_path)
                # فایل مستقیم (dropbox/gofile/pixeldrain/filelu/...) که واقعاً ویدیوئه
                # باید مثل ویدیوی معمولی رفتار کنه (پلیر این‌لاین)، نه کارت دانلود سند
                is_direct_video_file = job.file_type == FileType.DOCUMENT and _is_video_file(file_path)
                if is_image:
                    # پین عکسی (مثلاً Pinterest) - همیشه بر اساس پسوند واقعیِ فایل تشخیص
                    # داده می‌شه، چون تا قبل از دانلود واقعی نمی‌شه مطمئن بود پین
                    # عکسه یا ویدیو
                    caption = f"<b>🖼 {safe_title}</b>\n"
                    caption += caption_body
                    if job.detected_site:
                        caption += f"🔗 {job.detected_site}\n"
                elif job.file_type == FileType.AUDIO:
                    # ساخت caption برای موسیقی: فقط اطلاعات خودِ آهنگ (اسم/خواننده/
                    # آلبوم/مدت)، بدون caption_body. چون caption_body توضیحات کامل
                    # منبع اصلیه (مثلاً برای لینک‌های یوتیوب/یوتیوب‌میوزیک، کل
                    # توضیحات ویدیو با لینک‌های شبکه‌های اجتماعی، اعتبارات و
                    # هشتگ‌ها) که برای یه فایل موزیک معمولی به‌جای کپشن مفید،
                    # فقط شلوغی بی‌ربطه؛ کاربر این‌جا فقط اسم آهنگ رو می‌خواد.
                    caption = (
                        f"<b>🎵 {safe_title}</b>\n"
                        f"👤 {html.escape(job.artist or 'هنرمند نامشخص')}\n"
                    )
                    if job.album:
                        caption += f"💿 {html.escape(job.album)}\n"
                    if job.duration:
                        from datetime import timedelta
                        td = str(timedelta(seconds=job.duration))
                        caption += f"⏱ {td}\n"
                elif job.file_type == FileType.DOCUMENT and not is_direct_video_file:
                    # ساخت caption برای فایل مستقیم (گیت‌هاب و مشابه)
                    caption = f"<b>📦 {html.escape(job.file_name or job.title or 'فایل')}</b>\n"
                    caption += caption_body
                    if job.detected_site:
                        caption += f"🔗 {job.detected_site}\n"
                elif job.file_type == FileType.VIDEO_NOTE:
                    # پیام ویدیویی (گرد) توی تلگرام caption قبول نمی‌کنه
                    caption = None
                else:
                    # ساخت caption برای ویدیو (شامل ویدیوهای مستقیمِ dropbox/gofile/... هم می‌شه)
                    caption = f"<b>🎬 {safe_title}</b>\n"
                    if job.artist:
                        caption += f"👤 {html.escape(job.artist)}\n"
                    if job.duration:
                        from datetime import timedelta
                        td = str(timedelta(seconds=job.duration))
                        caption += f"⏱ {td}\n"
                    caption += caption_body
                    if job.detected_site:
                        caption += f"🔗 {job.detected_site}\n"

                # سقف واقعی کپشن تلگرام ۱۰۲۴ کاراکتره؛ اگه کل caption (تگ‌های HTML هم
                # حساب می‌شن) از این بیشتر بشه، ارسال فایل با ارور fail می‌کنه.
                # قبلاً این‌جا فقط برش می‌خورد و بقیه‌ی متن برای همیشه گم می‌شد؛
                # الان متن کامل رو نگه می‌داریم تا بعد از ارسال فایل، جدا (به‌عنوان
                # پیام متنی ریپلای‌شده) بفرستیمش - هیچ‌چیزی از کپشن واقعاً گم نمی‌شه.
                full_caption = caption
                if caption and len(caption) > 1024:
                    caption = caption[:1010].rstrip() + "…"
                
                # ارسال
                # اگه پیام اصلی کاربر هنوز موجود باشه، به همون ریپلای می‌زنیم تا تو گروه‌های
                # شلوغ معلوم باشه این فایل مالِ کدوم لینکه. اگه پیام اصلی پاک شده باشه
                # (خطای «message to be replied not found»)، بدون ریپلای می‌فرستیم.
                reply_to_message_id = job.message_id or None

                # افکت پیام (🎉 کانفتی) - از قابلیت‌های جدید تلگرامه، فقط توی چت خصوصی
                # کار می‌کنه (نه گروه/کانال)، برای همینم فقط وقتی chat_id مثبته
                # (یعنی چت خصوصیه، نه گروه/سوپرگروپ/کانال که ID منفی دارن) فعالش می‌کنیم.
                _CONFETTI_EFFECT_ID = "5046509860389126442"
                _effect_kwargs = {"message_effect_id": _CONFETTI_EFFECT_ID} if job.chat_id > 0 else {}

                async def _send_media(send_func, **kwargs):
                    """
                    ارسال فایل با سه تا fallback مستقل که تو یه حلقه‌ی واحد
                    مدیریت می‌شن (به‌جای تو‌درتو کردن try/except):
                    - اگه ریپلای به پیام اصلی رد شد (پاک شده)، بدون ریپلای امتحان می‌کنه
                    - اگه افکت کانفتی رد شد، بدون افکت امتحان می‌کنه
                    - اگه تلگرام Rate limit داد (TelegramRetryAfter)، طبق retry_after
                      صبر می‌کنه و دوباره امتحان می‌کنه (حداکثر ۳ بار)، به‌جای اینکه
                      آپلود رو بی‌سر و صدا از دست بده
                    """
                    use_reply = bool(reply_to_message_id)
                    use_effect = bool(_effect_kwargs)
                    base_kwargs = {**_effect_kwargs, **kwargs}

                    for attempt in range(4):
                        call_kwargs = dict(base_kwargs)
                        if not use_effect:
                            call_kwargs.pop("message_effect_id", None)
                        if use_reply:
                            call_kwargs["reply_to_message_id"] = reply_to_message_id
                        try:
                            return await send_func(**call_kwargs)
                        except TelegramRetryAfter as e:
                            wait = e.retry_after + 1
                            logger.warning(
                                f"Rate limit تلگرام موقع ارسال فایل job {job_id}، {wait} ثانیه صبر می‌کنیم (تلاش {attempt + 1})"
                            )
                            await asyncio.sleep(wait)
                            continue
                        except Exception as e:
                            err = str(e).lower()
                            if use_reply and ("not found" in err or "reply" in err):
                                logger.warning(
                                    f"ریپلای به پیام اصلی job {job_id} ممکن نشد، بدون ریپلای امتحان می‌شه: {e}"
                                )
                                use_reply = False
                                continue
                            if use_effect and "effect" in err:
                                logger.warning(f"افکت پیام برای job {job_id} رد شد، بدون افکت امتحان می‌شه: {e}")
                                use_effect = False
                                continue
                            raise
                    raise RuntimeError(f"ارسال فایل job {job_id} بعد از چند بار تلاش (rate limit) شکست خورد")

                # اگه کاربر «ارسال به‌صورت فایل/سند» رو روشن کرده، ویدیوهای معمولی
                # (نه عکس/صدا/video_note که معنی نداره) رو به‌جای پلیر این‌لاین
                # به‌صورت سند می‌فرستیم.
                force_document = (
                    user_force_document
                    and not is_image
                    and job.file_type not in (FileType.AUDIO, FileType.VIDEO_NOTE)
                )

                message = None
                if is_image:
                    message = await _send_media(
                        lambda **kw: bot.send_photo(
                            job.chat_id,
                            file_obj,
                            caption=caption,
                            parse_mode="HTML",
                            has_spoiler=user_spoiler,
                            **kw,
                        )
                    )
                elif job.file_type == FileType.AUDIO:
                    # عکس کاور برای پیش‌نمایش صوت تو تلگرام (جدا از --embed-thumbnail
                    # که تو خودِ فایل mp3 جاسازی شده). قبلاً job.thumbnail_url اصلاً
                    # این‌جا استفاده نمی‌شد، برای همین فایل صوتی تو چت بدون هیچ
                    # عکسی نشون داده می‌شد.
                    audio_thumb = None
                    if job.thumbnail_url:
                        try:
                            thumb_path = Path(file_path).parent / "audio_thumb.jpg"
                            async with httpx.AsyncClient(timeout=15.0) as client:
                                resp = await client.get(job.thumbnail_url)
                                if resp.status_code == 200:
                                    thumb_path.write_bytes(resp.content)
                                    audio_thumb = FSInputFile(thumb_path)
                        except Exception as e:
                            logger.warning(f"دانلود کاور صوت برای job {job_id} ناموفق بود: {e}")

                    message = await _send_media(
                        lambda **kw: bot.send_audio(
                            job.chat_id,
                            file_obj,
                            caption=caption,
                            parse_mode="HTML",
                            performer=job.artist or None,
                            title=job.title or file_path.rsplit('/', 1)[-1],
                            duration=job.duration or None,
                            thumbnail=audio_thumb,
                            **kw,
                        )
                    )
                elif force_document or (job.file_type == FileType.DOCUMENT and not is_direct_video_file):
                    # thumbnail سند: اول ترجیح با thumbnail دلخواه کاربره (منوی /settings)،
                    # وگرنه اگه ویدیویی بود که به‌زور به‌صورت سند می‌فرستیم (force_document)
                    # از thumb.jpg استخراج‌شده استفاده می‌کنیم.
                    thumb_path = Path(file_path).parent / "thumb.jpg"
                    doc_thumbnail = (
                        user_thumb_file_id if user_thumb_file_id
                        else (FSInputFile(thumb_path) if thumb_path.exists() else None)
                    )
                    message = await _send_media(
                        lambda **kw: bot.send_document(
                            job.chat_id,
                            file_obj,
                            caption=caption,
                            parse_mode="HTML",
                            thumbnail=doc_thumbnail,
                            **kw,
                        )
                    )
                elif job.file_type == FileType.VIDEO_NOTE:
                    message = await _send_media(
                        lambda **kw: bot.send_video_note(
                            job.chat_id,
                            file_obj,
                            length=384,
                            duration=job.duration if job.duration and job.duration <= 60 else None,
                            **kw,
                        )
                    )
                else:
                    # شامل ویدیوی معمولی و هم فایل مستقیمِ ویدیویی (dropbox/gofile/...)
                    # thumbnail دلخواه کاربر (اگه ست کرده باشه) اولویت داره، وگرنه
                    # همون thumb.jpg که موقع دانلود استخراج شده.
                    thumb_path = Path(file_path).parent / "thumb.jpg"
                    thumbnail = (
                        user_thumb_file_id if user_thumb_file_id
                        else (FSInputFile(thumb_path) if thumb_path.exists() else None)
                    )
                    message = await _send_media(
                        lambda **kw: bot.send_video(
                            job.chat_id,
                            file_obj,
                            caption=caption,
                            parse_mode="HTML",
                            supports_streaming=True,
                            thumbnail=thumbnail,
                            duration=job.duration or None,
                            has_spoiler=user_spoiler,
                            **kw,
                        )
                    )

                    # 📸 اسکرین‌شات پیش‌نمایش: علاوه بر خودِ ویدیو، همون فریمی که
                    # yt-dlp به‌عنوان thumbnail استخراج کرده رو (اگه کاربر تو
                    # /settings این گزینه رو روشن نگه داشته - پیش‌فرض روشنه) به‌صورت
                    # یه عکس جدا هم می‌فرستیم. اگه thumbnail دلخواه کاربر ست شده
                    # باشه، همونو نمی‌فرستیم (چون خودش قبلاً می‌دونه اون عکس چیه)،
                    # فقط وقتی از thumb.jpg خودِ ویدیو استفاده شده معنی داره.
                    if message and user_receive_screenshots and not user_thumb_file_id and thumb_path.exists():
                        try:
                            await bot.send_photo(
                                job.chat_id,
                                FSInputFile(thumb_path),
                                caption="📸 پیش‌نمایش",
                                reply_to_message_id=message.message_id,
                            )
                        except Exception as e:
                            logger.warning(f"ارسال اسکرین‌شات پیش‌نمایش برای job {job_id} ناموفق بود: {e}")

                # ری‌اکشن 👍 روی پیام اصلی کاربر بذاریم تا بفهمه لینکش پردازش شده
                if reply_to_message_id:
                    try:
                        await bot.set_message_reaction(
                            job.chat_id,
                            reply_to_message_id,
                            reaction=[ReactionTypeEmoji(emoji="👍")],
                        )
                    except Exception as e:
                        logger.warning(f"گذاشتن ری‌اکشن روی پیام اصلی job {job_id} ممکن نشد: {e}")

                # اگه کپشن واقعی از سقف ۱۰۲۴ کاراکتریِ کپشن تلگرام بلندتر بود و
                # بالاتر برش خورد، بقیه‌ش رو گم نمی‌کنیم - فقط ادامه‌ش (نه کل
                # کپشن از اول، که تکراری/عجیب می‌شد) رو جدا به‌عنوان پیام متنی
                # (ریپلای به همون فایل) می‌فرستیم. تلگرام رو پیام متنیِ معمولی
                # سقفش ۴۰۹۶ کاراکتره، پس اگه لازم بود چند تکه می‌کنیم.
                if message and full_caption and len(full_caption) > 1024:
                    remainder = full_caption[1010:].lstrip("… \n")
                    if remainder:
                        try:
                            first_chunk = True
                            for i in range(0, len(remainder), 4096):
                                chunk = remainder[i:i + 4096]
                                text = ("📝 <b>ادامه‌ی کپشن:</b>\n\n" + chunk) if first_chunk else chunk
                                first_chunk = False
                                await bot.send_message(
                                    job.chat_id,
                                    text,
                                    parse_mode="HTML",
                                    reply_to_message_id=message.message_id,
                                )
                        except Exception as e:
                            logger.warning(f"ارسال ادامه‌ی کپشنِ بلند برای job {job_id} ناموفق بود: {e}")

                # «خودتخریب» فایل - فقط برای دامنه‌هایی که ادمین صریحاً تو
                # /selfdestruct اضافه کرده (نه یوتیوب/اینستا/تیک‌تاک/آهنگ و بقیه)
                self_destruct_seconds = None
                if message:
                    sd_domains, sd_seconds = await _get_self_destruct_config(session)
                    if sd_domains and _url_matches_domain(job.url, sd_domains):
                        self_destruct_seconds = sd_seconds
                        try:
                            warn_msg = await bot.send_message(
                                job.chat_id,
                                f"⚠️ به‌دلایل امنیتی، این فایل تا <b>{self_destruct_seconds}</b> ثانیه‌ی "
                                f"دیگه خودکار از این چت پاک می‌شه.\n"
                                f"اگه لازمش داری، همین الان با «Forward» به Saved Messages "
                                f"(پیام‌های ذخیره‌شده‌ی خودت) بفرستش.",
                                reply_to_message_id=message.message_id,
                            )
                            delete_message_task.apply_async(
                                args=[job.chat_id, message.message_id], countdown=self_destruct_seconds
                            )
                            delete_message_task.apply_async(
                                args=[job.chat_id, warn_msg.message_id], countdown=self_destruct_seconds
                            )
                        except Exception as e:
                            logger.warning(f"زمان‌بندی خودتخریب برای job {job_id} ناموفق بود: {e}")

                if message:
                    # ذخیره file_id
                    file_id = None
                    if hasattr(message, 'audio') and message.audio:
                        file_id = message.audio.file_id
                    elif hasattr(message, 'video') and message.video:
                        file_id = message.video.file_id
                    elif hasattr(message, 'document') and message.document:
                        file_id = message.document.file_id
                    elif hasattr(message, 'photo') and message.photo:
                        file_id = message.photo[-1].file_id
                    elif hasattr(message, 'video_note') and message.video_note:
                        file_id = message.video_note.file_id
                    
                    # نوع واقعیِ فایل (چون برای Pinterest تا قبل از دانلود واقعی
                    # نمی‌شه مطمئن بود پین عکسه یا ویدیو)
                    actual_file_type = FileType.PHOTO if is_image else job.file_type

                    # بروزرسانی کار
                    try:
                        await DownloadJobCRUD.update_status(
                            session, job_id, DownloadStatus.DONE,
                            telegram_file_id=file_id,
                            file_type=actual_file_type,
                        )
                    except Exception as e:
                        # روی دیتابیس‌های قدیمی ستون telegram_file_id هنوز UNIQUE
                        # هست؛ دو job که همزمان همون مدیا رو فرستادن file_id یکسان
                        # می‌گیرن و آپدیت دوم IntegrityError می‌خوره. فایل به کاربر
                        # رسیده پس job باید DONE بشه نه FAILED - بدون file_id.
                        await session.rollback()
                        logger.warning(
                            f"ذخیره‌ی telegram_file_id برای job {job_id} ناموفق بود "
                            f"(احتمالاً constraint قدیمی UNIQUE): {e}"
                        )
                        await DownloadJobCRUD.update_status(
                            session, job_id, DownloadStatus.DONE,
                            file_type=actual_file_type,
                        )
                    
                    # ثبت در cache - به‌جز محتوای خودتخریب، چون کش کردنش دقیقاً هدف
                    # خودتخریب (کم‌کردن ردپا/جلوگیری از توزیع آسون‌تر) رو خنثی می‌کنه
                    if not self_destruct_seconds:
                        cache_hash = get_cache_hash(job.url, job.selected_quality or "best", actual_file_type.value)
                        await FileCacheCRUD.create(
                            session,
                            url_hash=cache_hash,
                            source_url=job.url,
                            quality=job.selected_quality or "best",
                            telegram_file_id=file_id,
                            file_type=actual_file_type,
                            file_name=job.file_name,
                            title=job.title,
                            artist=job.artist,
                            album=job.album,
                            duration=job.duration,
                            description=job.description,
                        )
                    
                    # آمار
                    await DownloadStatsCRUD.increment_today(
                        session,
                        successful=True,
                        size_mb=job.file_size_mb or 0,
                        site=job.detected_site
                    )
                    
                    logger.info(f"✅ فایل ارسال شد (job {job_id})")
                    await _delete_status_message(job)
                    return file_id
            
            except Exception as e:
                logger.error(f"خطا در ارسال: {e}", exc_info=True)
                await DownloadJobCRUD.update_status(
                    session, job_id, DownloadStatus.FAILED,
                    error_msg=f"خطا در ارسال: {str(e)[:255]}"
                )
                await DownloadStatsCRUD.increment_today(session, successful=False, size_mb=job.file_size_mb or 0, site=job.detected_site)

                # اگه خطا مربوط به رد شدن از سقف حجم خودِ تلگراف باشه، پیام واضح‌تری بدیم
                err_text = str(e).lower()
                if "entity too large" in err_text or "too big" in err_text or "413" in err_text:
                    reason = (
                        f"📦 فایل (<b>{job.file_size_mb:.1f} مگابایت</b> بود، ولی) "
                        "بیشتر از سقفی هست که تلگرام برای ارسال بات‌ها اجازه می‌ده.\n"
                        "لطفاً کیفیت پایین‌تری رو امتحان کن."
                    )
                else:
                    reason = "فایل دانلود شد ولی توی ارسال به تلگرام خطا پیش اومد."
                await _notify_failure(job.chat_id, job_id, reason, message_id=job.message_id)
                await _delete_status_message(job)

            finally:
                # فایل چه با موفقیت ارسال بشه چه نشه، دیگه لازم نیست روی دیسک
                # سرور بمونه - همینجا پاکش می‌کنیم تا فضا پر نشه
                _remove_job_dir(job_id)

    return run_async(_send())


@celery_app.task(queue='default')
def delete_message_task(chat_id: int, message_id: int):
    """
    حذف یه پیام خاص با تأخیر (برای قابلیت «خودتخریب» فایل‌های حساس).
    یه تسک سبک و جدا از تسک اصلی دانلود/آپلوده، چون با apply_async(countdown=...)
    زمان‌بندی می‌شه و نباید ورکر اصلی رو برای چند دقیقه صرفاً منتظر نگه داره.
    """
    async def _delete():
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception as e:
            logger.debug(f"حذف خودکار پیام {message_id} تو چت {chat_id} ممکن نشد (شاید قبلاً پاک شده): {e}")

    return run_async(_delete())


@celery_app.task(queue='default')
def cleanup_direct_link(token: str):
    """پاک‌سازی خودکارِ یه لینک دانلود مستقیم بعد از انقضا (DIRECT_LINK_EXPIRE_SECONDS).

    قبل از پاک کردن واقعی، از دیتابیس چک می‌کنه که آیا کاربر دستی تمدیدش کرده
    (دکمه‌ی «تمدید لینک») - اگه زمان انقضای ذخیره‌شده هنوز نرسیده، به‌جای پاک
    کردن، خودش رو دوباره برای همون زمان باقی‌مونده زمان‌بندی می‌کنه. این‌جوری
    برای تمدید نیازی به لغو/ردیابی دستیِ تسک celery اصلی نیست."""
    from datetime import datetime
    from utils.direct_links import delete_direct_link

    async def _check_and_maybe_reschedule() -> bool:
        """True یعنی رزرو دوباره شد (پاک نکن)، False یعنی واقعاً منقضی شده."""
        async with async_session_maker() as session:
            job = await DownloadJobCRUD.get_by_direct_link_token(session, token)
            if not job or not job.direct_link_expires_at:
                return False
            remaining = (job.direct_link_expires_at - datetime.utcnow()).total_seconds()
            if remaining > 5:
                cleanup_direct_link.apply_async(args=[token], countdown=int(remaining) + 5)
                return True
            await DownloadJobCRUD.clear_direct_link(session, job.id)
            return False

    rescheduled = run_async(_check_and_maybe_reschedule())
    if not rescheduled:
        delete_direct_link(token)


@celery_app.task(queue='default')
def poll_playlist_progress(playlist_job_id: int, playlist_batch_id: str, _tick: int = 0):
    """
    گزارش پیشرفت تجمیعی یه پلی‌لیست/آلبوم اسپاتیفای (X از N دانلود شد، Y ناموفق).

    چون معماری پروژه «یه job = یه فایل»ـه، هر ترک پلی‌لیست خودش یه job و یه
    پیام وضعیت جدا داره - بدون این تسک، هیچ‌جا خلاصه‌ی کل پلی‌لیست دیده
    نمی‌شد و کاربر باید دستی همه‌ی پیام‌ها رو می‌شمرد. این تسک هر ۸ ثانیه
    خودش رو با apply_async(countdown=...) دوباره زمان‌بندی می‌کنه (دقیقاً
    همون الگوی cleanup_direct_link) تا وقتی همه‌ی ترک‌ها به وضعیت نهایی
    (done/failed/cancelled) برسن، یا یه سقف ایمنی (۱۵ دقیقه) رد بشه - که
    جلوی حلقه‌ی ابدی رو می‌گیره اگه یه job به هر دلیلی گیر کنه و هیچ‌وقت
    finalize نشه.
    """
    _MAX_TICKS = 112  # ~15 دقیقه با فاصله‌ی ۸ ثانیه‌ای

    async def _update():
        async with async_session_maker() as session:
            job = await DownloadJobCRUD.get_by_id(session, playlist_job_id)
            if not job or not job.status_message_id:
                return False  # job والد یا پیام وضعیتش دیگه وجود نداره، ادامه نده

            progress = await DownloadJobCRUD.get_playlist_progress(session, playlist_batch_id)
            total = progress["total"]
            done = progress["done"]
            failed = progress["failed"]
            finished = progress["finished"]

            if total == 0:
                return False

            percent = (finished / total) * 100
            is_complete = finished >= total or _tick >= _MAX_TICKS

            if is_complete:
                text = (
                    f"🎧 دانلود پلی‌لیست/آلبوم تموم شد\n"
                    f"{_progress_bar(100 if finished >= total else percent)}\n"
                    f"✅ {done} تا موفق"
                    + (f" | ❌ {failed} تا ناموفق" if failed else "")
                    + (f" | ⏳ {total - finished} تا هنوز در حال دانلود" if finished < total else "")
                )
            else:
                text = (
                    f"🎧 در حال دانلود پلی‌لیست/آلبوم...\n"
                    f"{_progress_bar(percent)}\n"
                    f"✅ {done} موفق"
                    + (f" | ❌ {failed} ناموفق" if failed else "")
                    + f" (از {total} آهنگ)"
                )

            try:
                await bot.edit_message_text(
                    text, chat_id=job.chat_id, message_id=job.status_message_id,
                )
            except Exception:
                pass  # متن فرقی نکرده یا پیام پاک شده - مهم نیست

            return not is_complete  # True یعنی باید دوباره زمان‌بندی بشه

    should_continue = run_async(_update())
    if should_continue:
        poll_playlist_progress.apply_async(
            args=[playlist_job_id, playlist_batch_id, _tick + 1], countdown=8
        )


async def _fail_stale_jobs_async(older_than_minutes: int = 120) -> int:
    async with async_session_maker() as session:
        return await DownloadJobCRUD.fail_stale_jobs(session, older_than_minutes=older_than_minutes)


@celery_app.task(queue='default')
def cleanup_temp_files():
    """پاک‌سازی فایل‌های مؤقت قدیم"""
    logger.info("شروع پاک‌سازی فایل‌های مؤقت...")

    import shutil
    from datetime import datetime, timedelta

    # جاب‌های زامبی: هر جابی که به‌خاطر کرش worker یا گم‌شدن تسک تو Redis
    # برای بیش از ۲ ساعت تو وضعیت "در حال پردازش" گیر کرده، رو FAILED
    # می‌کنیم - وگرنه برای همیشه تو شمارنده‌ی صف (queue position) حساب
    # می‌شن و کاربر جدید فکر می‌کنه صف شلوغه در حالی که خبری نیست.
    try:
        stale_count = run_async(_fail_stale_jobs_async())
        if stale_count:
            logger.info(f"{stale_count} جاب زامبی/گیرکرده FAILED شد")
    except Exception as e:
        logger.error(f"خطا در پاک‌سازی جاب‌های زامبی: {e}")

    temp_dir = Path(settings.TEMP_DIR)
    if not temp_dir.exists():
        return
    
    # حذف پوشه‌هایی که بیش از 24 ساعت قدیمی‌تر هستند
    cutoff_time = datetime.now() - timedelta(hours=24)
    
    for folder in temp_dir.iterdir():
        if folder.is_dir():
            mtime = datetime.fromtimestamp(folder.stat().st_mtime)
            if mtime < cutoff_time:
                try:
                    shutil.rmtree(folder)
                    logger.info(f"حذف شد: {folder.name}")
                except Exception as e:
                    logger.error(f"خطا در حذف {folder.name}: {e}")

    # سیف‌نت برای لینک‌های دانلود مستقیم: پاک‌سازی هر لینک معمولاً با یه Celery
    # task زمان‌بندی‌شده (apply_async countdown=DIRECT_LINK_EXPIRE_SECONDS) انجام
    # می‌شه، اما اگه Redis ری‌استارت بشه یا اون تسک قبل از موعدش گم بشه، فایل
    # رو دیسک می‌مونه برای همیشه. اینجا هر پوشه‌ی توکن‌دار قدیمی‌تر از انقضا
    # (+ یه حاشیه‌ی امن) رو مستقل از اون تسک، به‌صورت دستی پاک می‌کنیم.
    if settings.DIRECT_LINK_ENABLED and settings.DIRECT_LINK_DIR:
        direct_link_dir = Path(settings.DIRECT_LINK_DIR)
        if direct_link_dir.exists():
            expire_cutoff = datetime.now() - timedelta(seconds=settings.DIRECT_LINK_EXPIRE_SECONDS + 600)
            for folder in direct_link_dir.iterdir():
                if folder.is_dir():
                    try:
                        mtime = datetime.fromtimestamp(folder.stat().st_mtime)
                        if mtime < expire_cutoff:
                            shutil.rmtree(folder)
                            logger.info(f"لینک مستقیم یتیم/جاماندە پاک شد: {folder.name}")
                    except Exception as e:
                        logger.error(f"خطا در پاک‌سازی لینک مستقیم {folder.name}: {e}")

    # Local Bot API Server هر فایل آپلودی/دانلودی رو زیر
    # BOT_API_LOCAL_FILES_DIR/<token>/(videos|documents|photos|...)‌ برای
    # همیشه نگه می‌داره و خودش هیچ‌وقت پاکش نمی‌کنه. اگه پاکش نکنیم، دقیقاً
    # همون فایلی که تو TEMP_DIR/DIRECT_LINK_DIR داریم، اینجا هم یه کپی داره
    # و دیسک رو دوبرابر سریع‌تر پر می‌کنه. فایل‌های قدیمی‌تر از
    # BOT_API_LOCAL_FILES_MAX_AGE_HOURS رو پاک می‌کنیم (فقط فایل، نه خود
    # پوشه‌ها، چون خود سرور به ساختار پوشه‌ها نیاز داره).
    if settings.USE_LOCAL_BOT_API_SERVER and settings.BOT_API_LOCAL_FILES_DIR:
        bot_api_dir = Path(settings.BOT_API_LOCAL_FILES_DIR)
        if bot_api_dir.exists():
            bot_api_cutoff = datetime.now() - timedelta(hours=settings.BOT_API_LOCAL_FILES_MAX_AGE_HOURS)
            for file_path in bot_api_dir.rglob("*"):
                if file_path.is_file():
                    try:
                        mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
                        if mtime < bot_api_cutoff:
                            file_path.unlink()
                            logger.info(f"فایل کشِ Local Bot API پاک شد: {file_path}")
                    except Exception as e:
                        logger.error(f"خطا در پاک‌سازی فایل کش Local Bot API {file_path}: {e}")


def _disk_usage_percent_for(paths: list[str]) -> Optional[float]:
    """بیشترین درصدِ پر بودن دیسکِ میزبانِ مسیرهای داده‌شده (فقط مسیرهای
    قابل‌اندازه‌گیری). None یعنی هیچ مسیری قابل اندازه‌گیری نبود."""
    percents = []
    for raw in paths:
        if not raw:
            continue
        try:
            p = Path(raw)
            p.mkdir(parents=True, exist_ok=True)
            total, used, _free = shutil.disk_usage(p)
            if total > 0:
                percents.append(used / total * 100)
        except Exception as e:
            logger.debug(f"اندازه‌گیری فضای دیسک برای {raw} ناموفق بود: {e}")
    return max(percents) if percents else None


@celery_app.task(queue='default')
def check_disk_space():
    """نگهبان فضای دیسک - هر ۱۵ دقیقه (beat).

    این پروژه دو بار واقعاً به‌خاطر «دیسک پر شد» از کار افتاد (کامنت‌های
    _download_with_progress و cleanup_temp_files رو ببین)؛ پاک‌سازی‌های خودکار
    علت‌ها رو کم کردن، ولی هیچ‌چیز «پیش از فاجعه» خبر نمی‌داد. این تسک:
      - بالای DISK_WARN_PERCENT (پیش‌فرض ۸۵٪): یه‌بار به ادمین‌ها هشدار می‌ده
      - بالای DISK_CRITICAL_PERCENT (پیش‌فرض ۹۵٪): state رو critical می‌کنه -
        بات (bot/main.py) بر اساس همین state، دانلودهای جدید رو موقتاً می‌بنده
      - زیر DISK_RESUME_PERCENT (پیش‌فرض ۸۰٪): برگشت به عادی + پیام
    فقط روی *تغییر* state پیام می‌ده (هیسترزیس + بدون اسپم)."""
    async def _run():
        paths = [settings.TEMP_DIR]
        if settings.DIRECT_LINK_ENABLED and settings.DIRECT_LINK_DIR:
            paths.append(settings.DIRECT_LINK_DIR)
        if settings.USE_LOCAL_BOT_API_SERVER and settings.BOT_API_LOCAL_FILES_DIR:
            paths.append(settings.BOT_API_LOCAL_FILES_DIR)

        worst = _disk_usage_percent_for(paths)
        if worst is None:
            logger.warning("نگهبان دیسک: هیچ مسیری برای اندازه‌گیری فضای دیسک در دسترس نبود")
            return

        async with async_session_maker() as session:
            prev = await BotConfigCRUD.get(session, "disk_space_state") or "ok"

            if worst >= settings.DISK_CRITICAL_PERCENT:
                new_state, msg = "critical", None
            elif worst >= settings.DISK_WARN_PERCENT:
                new_state, msg = "warn", None
            elif worst <= settings.DISK_RESUME_PERCENT:
                new_state, msg = "ok", None
            else:
                # بین آستانه‌ی برگشت و آستانه‌ی هشدار - state فعلی بمونه، پیامی هم نرو
                return

            if new_state == prev:
                return
            await BotConfigCRUD.set(session, "disk_space_state", new_state)

        if new_state == "critical":
            msg = (
                f"🚨 دیسک سرور به {worst:.0f}% پریده! دانلودهای جدید موقتاً بسته شد.\n"
                "پاک‌سازی کن (فایل‌های Local Bot API / direct_links / TEMP_DIR) یا فضا اضافه کن."
            )
        elif new_state == "warn":
            msg = f"⚠️ فضای دیسک سرور به {worst:.0f}% رسیده - بهتره قبل از پر شدن پاک‌سازی کنی."
        elif new_state == "ok":
            msg = f"✅ فضای دیسک سرور به حالت عادی برگشت ({worst:.0f}%). دانلودها دوباره بازه."

        logger.warning(f"نگهبان دیسک: state={new_state} (پرترین دیسک: {worst:.1f}%)")
        for admin_id in settings.ADMIN_IDS:
            try:
                await bot.send_message(admin_id, msg)
            except Exception as e:
                logger.warning(f"ارسال هشدار دیسک به ادمین {admin_id} ناموفق بود: {e}")

    run_async(_run())


@celery_app.task(queue='default')
def cleanup_old_jobs():
    """حذف خودکار تاریخچه‌ی قدیمی (روزانه، beat).

    جدول download_jobs با هر پیام لینک INSERT و چند SELECT می‌خوره؛ بدون
    پاک‌سازی، بعد از چند صد هزار رکورد SQLite به‌مرور کند می‌شه. نگه‌داری با
    JOB_RETENTION_DAYS کنترل می‌شه (پیش‌فرض ۹۰ روز؛ ۰ = غیرفعال). آمار روزانه
    و کش فایل‌ها دست‌نخورده می‌مونن."""
    if settings.JOB_RETENTION_DAYS <= 0:
        return

    async def _run():
        async with async_session_maker() as session:
            deleted = await DownloadJobCRUD.archive_old_jobs(
                session, older_than_days=settings.JOB_RETENTION_DAYS
            )
        if deleted:
            logger.info(
                f"{deleted} job قدیمی‌تر از {settings.JOB_RETENTION_DAYS} روز حذف شد "
                f"(نگه‌داری تاریخچه)"
            )

    run_async(_run())


@celery_app.task(queue='default')
def daily_error_digest():
    """گزارش روزانه‌ی خطاهای سرور برای ادمین‌ها (beat، هر روز ۹:۰۰ UTC).

    فایل errors_دیروز.log (که loguru با سطح ERROR پر می‌کنه) رو می‌خونه،
    خطاها رو بر اساس «ماژول:خط + شروع پیام» گروه‌بندی می‌کنه و ۵ الگوی
    پربسامد رو با شمارش می‌فرسته.

    - روزِ بی‌خطا پیامی نمی‌فرسته (بدون اسپم) - یعنی سکوت = همه‌چیز سالمه
    - تو حالت Docker، پوشه‌ی logs بین bot و worker مشترکه (ولوم logs_data)
      تا خطاهای بات هم دیده بشن"""
    from collections import Counter

    yesterday = (datetime.utcnow().date() - timedelta(days=1)).isoformat()
    log_path = Path("logs") / f"errors_{yesterday}.log"
    if not log_path.exists():
        return

    groups: Counter = Counter()
    total = 0
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                # فرمت loguru: time | LEVEL | name:line | message
                # خطوط ادامه‌ی backtrace شکل استاندارد ندارن و رد می‌شن
                parts = [p.strip() for p in line.split("|", 3)]
                if len(parts) != 4 or parts[1] not in ("ERROR", "CRITICAL"):
                    continue
                total += 1
                key = f"{parts[2]} — {parts[3][:100]}".replace("\n", " ")
                groups[key] += 1
    except Exception as e:
        logger.warning(f"خوندن فایل خطاهای دیروز ({log_path}) ناموفق بود: {e}")
        return

    if total == 0:
        return

    lines = [
        f"🛠 <b>گزارش خطاهای {yesterday}</b>",
        f"مجموع: <b>{total}</b> خطا",
        "",
    ]
    for key, count in groups.most_common(5):
        lines.append(f"• {count}× <code>{html.escape(key)}</code>")
    if len(groups) > 5:
        lines.append(f"… و {len(groups) - 5} الگوی دیگر")
    digest_text = "\n".join(lines)

    async def _send():
        for admin_id in settings.ADMIN_IDS:
            try:
                await bot.send_message(admin_id, digest_text)
            except Exception as e:
                logger.warning(f"ارسال گزارش خطای روزانه به ادمین {admin_id} ناموفق بود: {e}")

    run_async(_send())


@celery_app.task(queue='default')
def update_ytdlp():
    """
    آپدیت خودکار ابزارهای دانلود روی خودِ سرور (بدون Docker).
    چون این پروژه با systemd/run.py مستقیم رو سرور اجرا می‌شه (نه ایمیج Docker
    که هر بار از نو build بشه)، نسخه‌ی این ابزارهای پین‌شده تو requirements.txt
    با گذشت زمان قدیمی می‌مونن و همون خطاهای آشنای «login required» / 403 برای
    ساندکلود، اینستاگرام، اسپاتیفای و غیره برمی‌گرده - چون این پلتفرم‌ها مدام
    ساختار سایتشون رو عوض می‌کنن و این ابزارها باید مدام دنبالشون آپدیت بشن.

    - yt-dlp: اصلی‌ترین موتور دانلود، برای همه‌ی سایت‌ها از جمله دانلود صدا
      از یوتیوب برای اسپاتیفای (متادیتای اسپاتیفای از تگ‌های meta خودِ صفحه
      گرفته می‌شه، نه از این ابزار).
    - curl_cffi: برای گرفتن HTML صفحه‌ی اسپاتیفای با TLS fingerprint مرورگر
      واقعی - چون اسپاتیفای/Akamai مدام مکانیزم ضد-بات‌شون رو تغییر می‌دن.

    با تنظیم AUTO_UPDATE_YTDLP=false در .env قابل خاموش‌کردنه - اگه می‌خواید
    نسخه‌ها کاملاً پین بمونن (آپگرید وسط اجرا ممکنه رفتار دانلود رو عوض کنه).

    ffmpeg رو عمداً اینجا آپدیت نمی‌کنیم: رو لینوکس معمولاً از پکیج‌منیجر
    سیستم (apt/dnf) نصب می‌شه، نه pip، و آپدیت خودکارش از داخل یه تسک
    Celery بدون sudo عملاً ممکن نیست؛ نسخه‌ش رو فقط برای دیدِ ادمین لاگ
    می‌کنیم تا اگه خیلی قدیمی بود، دستی آپدیتش کنه.

    این تسک با همون venv/pythonی که خود ربات باهاش اجرا می‌شه (sys.executable)
    آپدیتشون می‌کنه، بدون نیاز به دخالت دستی. ادمین‌ها فقط وقتی نسخه‌ی
    yt-dlp واقعاً عوض شده باشه مطلع می‌شن (نه هر ۶ ساعت که هیچ تغییری نبوده)،
    تا اسپم نشن.
    """
    import subprocess as sp

    if not settings.AUTO_UPDATE_YTDLP:
        logger.info("AUTO_UPDATE_YTDLP خاموش است - آپدیت خودکار ابزارها رد شد")
        return

    def _pip_upgrade(package: str) -> tuple[bool, str]:
        try:
            result = sp.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", "--no-input", package],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                return True, result.stdout[-500:]
            return False, result.stderr[-500:]
        except Exception as e:
            return False, str(e)

    def _ytdlp_version() -> Optional[str]:
        try:
            result = sp.run(
                [sys.executable, "-m", "yt_dlp", "--version"],
                capture_output=True, text=True, timeout=30,
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except Exception:
            return None

    logger.info("شروع آپدیت خودکار ابزارهای دانلود (yt-dlp, curl_cffi)...")

    old_version = _ytdlp_version()
    ok, output = _pip_upgrade("yt-dlp")
    if ok:
        logger.info(f"آپدیت yt-dlp موفق:\n{output}")
    else:
        logger.warning(f"آپدیت yt-dlp ناموفق:\n{output}")
    new_version = _ytdlp_version()

    ok_sp, output_sp = _pip_upgrade("curl_cffi")
    if ok_sp:
        logger.info(f"آپدیت curl_cffi موفق:\n{output_sp}")
    else:
        logger.warning(f"آپدیت curl_cffi ناموفق:\n{output_sp}")

    # لاگ نسخه‌ی ffmpeg برای دید ادمین (بدون تلاش برای آپدیت خودکارش)
    try:
        ffmpeg_result = sp.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=10)
        ffmpeg_first_line = (ffmpeg_result.stdout or "").splitlines()[0] if ffmpeg_result.stdout else "نامشخص"
        logger.info(f"نسخه‌ی فعلی ffmpeg: {ffmpeg_first_line}")
    except Exception as e:
        logger.warning(f"چک نسخه‌ی ffmpeg ناموفق بود: {e}")

    if old_version and new_version and old_version != new_version:
        logger.info(f"yt-dlp از نسخه‌ی {old_version} به {new_version} آپدیت شد")

        async def _notify():
            for admin_id in settings.ADMIN_IDS:
                try:
                    await bot.send_message(
                        admin_id,
                        f"🔄 yt-dlp خودکار آپدیت شد: {old_version} ← {new_version}",
                    )
                except Exception as e:
                    logger.warning(f"اطلاع‌رسانی آپدیت yt-dlp به ادمین {admin_id} ناموفق بود: {e}")

        run_async(_notify())


@celery_app.task(queue='default')
def check_instagram_cookie():
    """
    چک فعال سلامت کوکی اینستاگرام (نه فقط سن فایل - چون یه کوکی ۲روزه هم
    مرده دیده شده). این تسک هر چند ساعت یه‌بار واقعاً با اینستاگرام تست
    می‌کنه و فقط وقتی وضعیت از سالم به خراب (یا برعکس) عوض بشه به ادمین‌ها
    پیام می‌ده - نه هر بار - تا اسپم نشه.
    """
    if not settings.INSTAGRAM_COOKIE_FILE:
        return

    from utils.instagram_downloader import check_cookie_health

    async def _check():
        healthy, detail = await check_cookie_health(settings.INSTAGRAM_COOKIE_FILE)
        async with async_session_maker() as session:
            was_broken = await BotConfigCRUD.get_bool(session, "instagram_cookie_broken", False)
            if not healthy and not was_broken:
                logger.warning(f"کوکی اینستاگرام خراب شد: {detail}")
                await BotConfigCRUD.set(session, "instagram_cookie_broken", "true")
                for admin_id in settings.ADMIN_IDS:
                    try:
                        await bot.send_message(
                            admin_id,
                            f"🍪 کوکی اینستاگرام از کار افتاد: {detail}\n"
                            "لطفاً از یه مرورگر لاگین‌شده export کن و رو سرور جایگزین کن."
                        )
                    except Exception as e:
                        logger.warning(f"ارسال هشدار کوکی اینستاگرام به ادمین {admin_id} ناموفق بود: {e}")
            elif healthy and was_broken:
                logger.info("کوکی اینستاگرام دوباره سالم شد")
                await BotConfigCRUD.set(session, "instagram_cookie_broken", "false")
                for admin_id in settings.ADMIN_IDS:
                    try:
                        await bot.send_message(admin_id, "✅ کوکی اینستاگرام دوباره سالم شد.")
                    except Exception as e:
                        logger.warning(f"ارسال پیام سلامت کوکی اینستاگرام به ادمین {admin_id} ناموفق بود: {e}")

    run_async(_check())
