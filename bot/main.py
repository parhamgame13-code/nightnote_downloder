"""
🤖 ربات دانلودر گروپی
نقطه ورود اصلی
"""
import asyncio
import html
import json
import re
import logging
from typing import Optional
from urllib.parse import urlparse
from loguru import logger

from aiogram import Bot, Dispatcher, F, types, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums.parse_mode import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.exceptions import TelegramRetryAfter
# SkipHandler تو aiogram.exceptions نیست - مسیر درستش همینه. raise کردنش از
# داخل یه هندلر یعنی «این آپدیت رو من مصرف نکردم، برو هندلر بعدی» - بدونش
# هر هندلری که match بشه (حتی با return ساده) propagation رو متوقف می‌کنه.
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ReactionTypeEmoji

from bot.config import settings, build_bot_session
from bot.premium_emoji_middleware import PremiumEmojiRequestMiddleware
from db.session import init_db, close_db
from db.crud import UserCRUD
from db.models import FileType, DownloadStatus
from utils.link_detector import LinkDetector, SiteType
from utils.url_safety import url_is_safe_to_fetch
from utils.metadata import MetadataExtractor, format_metadata_message
from utils.texts import EDITABLE_TEXTS, get_text
from utils.premium_emoji import (
    get_registered_emojis,
    is_premium_active,
    register_emoji,
    set_premium_active,
    unregister_emoji,
)
from worker.tasks import download_file, download_uploaded_file
from worker.celery_app import celery_app

from utils.logger import setup_logger

setup_logger()

bot = Bot(
    token=settings.BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    session=build_bot_session(),
)
bot.session.middleware(PremiumEmojiRequestMiddleware())
dp = Dispatcher()


_bot_username: str | None = None
_COMMAND_RE = re.compile(r"^/([a-zA-Z0-9_]+)(?:@([a-zA-Z0-9_]+))?")


class _CommandReactionMiddleware(BaseMiddleware):
    """
    رو دستوری که خطاب به همین باته - یعنی یا دستور خام بدون @یوزرنیم (تو پیوی
    یا وقتی فقط یه بات تو گروپه)، یا دستوری که دقیقاً @یوزرنیم خودِ این بات رو
    صدا زده (مثل /start@NightNote_DL_bot) - یه ری‌اکشن ❤️ می‌ذاره. قبلاً رو
    *هر* پیامی که با "/" شروع می‌شد ری‌اکشن می‌ذاشت، حتی دستوری که تو یه گروپ
    چندبات صریحاً یه بات دیگه رو صدا زده بود (مثلاً /start@SomeOtherBot) - که
    اشتباه بود. چون middlewareه نه یه هندلر جدا، جلوی اجرای هندلر اصلی رو
    نمی‌گیره.
    """

    async def __call__(self, handler, event: types.Message, data: dict):
        if event.text:
            match = _COMMAND_RE.match(event.text)
            if match:
                mentioned_username = match.group(2)
                targets_this_bot = (
                    mentioned_username is None
                    or (_bot_username is not None and mentioned_username.lower() == _bot_username.lower())
                )
                if targets_this_bot:
                    try:
                        await bot.set_message_reaction(
                            event.chat.id, event.message_id, reaction=[ReactionTypeEmoji(emoji="❤")]
                        )
                    except Exception as e:
                        logger.debug(f"گذاشتن ری‌اکشن رو دستور ممکن نشد (بی‌اهمیت): {e}")
        return await handler(event, data)


dp.message.outer_middleware(_CommandReactionMiddleware())


async def _find_unjoined_channels(user_id: int) -> list:
    """کانال‌های اجباری که کاربر هنوز عضوشون نیست رو برمی‌گردونه (خالی = همه‌جا عضوه)"""
    from db.session import async_session_maker
    async with async_session_maker() as session:
        channels = await _get_force_join_channels(session)
    if not channels:
        return []

    not_joined = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch["id"], user_id)
            if member.status in ("left", "kicked"):
                not_joined.append(ch)
        except Exception as e:
            logger.warning(f"چک عضویت کانال {ch.get('id')} ناموفق بود (شاید بات ادمین کانال نیست): {e}")
            # اگه نشد چک کنیم، به نفع سخت‌گیری می‌گیریم که عضو نیست - تا ادمین
            # متوجه‌ی مشکلِ تنظیمات (مثلاً بات ادمین کانال نیست) بشه
            not_joined.append(ch)
    return not_joined


async def _send_join_prompt(message: types.Message, channels: list):
    rows = []
    for ch in channels:
        link = ch.get("link") or f"https://t.me/{str(ch['id']).lstrip('@')}"
        rows.append([InlineKeyboardButton(text=f"📢 {ch.get('title') or ch['id']}", url=link)])
    rows.append([InlineKeyboardButton(text="✅ عضو شدم", callback_data="checkjoin", style="success")])
    from db.session import async_session_maker
    async with async_session_maker() as session:
        text = await get_text(session, "text_force_join_prompt")
    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


class _ForceJoinMiddleware(BaseMiddleware):
    """
    قبل از اجرای هر هندلری تو پیوی، چک می‌کنه کاربر عضو کانال‌های اجباریه یا نه.
    برخلاف میدلورِ ری‌اکشن (که همیشه ادامه می‌ده)، این یکی اگه کاربر عضو نباشه
    اصلاً handler() رو صدا نمی‌زنه - یعنی واقعاً جلوی اجرای دستور/لینک رو می‌گیره.
    """

    async def __call__(self, handler, event: types.Message, data: dict):
        if event.chat.type == "private" and not _is_admin(event.from_user.id):
            not_joined = await _find_unjoined_channels(event.from_user.id)
            if not_joined:
                await _send_join_prompt(event, not_joined)
                return
        return await handler(event, data)


class _ForceJoinCallbackMiddleware(BaseMiddleware):
    """همون چک عضویت، برای دکمه‌های شیشه‌ای (کیفیت/لغو/...) - جز خودِ دکمه‌ی چک عضویت"""

    async def __call__(self, handler, event: CallbackQuery, data: dict):
        if event.data == "checkjoin" or _is_admin(event.from_user.id):
            return await handler(event, data)
        if event.message and event.message.chat.type == "private":
            not_joined = await _find_unjoined_channels(event.from_user.id)
            if not_joined:
                await event.answer("اول باید عضو کانال‌های موردنیاز بشی. /start رو دوباره بزن.", show_alert=True)
                return
        return await handler(event, data)


dp.message.outer_middleware(_ForceJoinMiddleware())
dp.callback_query.outer_middleware(_ForceJoinCallbackMiddleware())


@dp.callback_query(F.data == "checkjoin")
async def checkjoin_callback(callback: CallbackQuery):
    not_joined = await _find_unjoined_channels(callback.from_user.id)
    if not_joined:
        await callback.answer("هنوز عضو همه‌ی کانال‌ها نشدی!", show_alert=True)
        return
    await callback.answer("✅ عضویت تایید شد!")
    try:
        await callback.message.delete()
    except Exception:
        pass
    from db.session import async_session_maker
    async with async_session_maker() as session:
        text = await get_text(session, "text_joined_confirm")
    await callback.message.answer(text)


@dp.message(Command("forcejoin"))
async def forcejoin_panel_handler(message: types.Message):
    """پنل مدیریت عضویت اجباری - فقط ادمین"""
    if not _is_admin(message.from_user.id):
        return

    from db.session import async_session_maker
    async with async_session_maker() as session:
        channels = await _get_force_join_channels(session)

    if channels:
        lines = [f"{i + 1}. {ch.get('title') or ch['id']} (<code>{ch['id']}</code>)" for i, ch in enumerate(channels)]
        listing = "کانال‌های فعلی:\n" + "\n".join(lines)
    else:
        listing = "هنوز هیچ کانالی تنظیم نشده."

    rows = [[InlineKeyboardButton(text="➕ افزودن کانال", callback_data="fj_add", style="success")]]
    for i, ch in enumerate(channels):
        rows.append([InlineKeyboardButton(
            text=f"❌ حذف: {ch.get('title') or ch['id']}", callback_data=f"fj_remove:{i}", style="danger"
        )])

    await message.answer(
        f"<b>📢 عضویت اجباری</b>\n\n{listing}\n\n"
        "⚠️ برای هر کانال، خودِ بات باید ادمین اون کانال باشه، وگرنه نمی‌تونه عضویت کاربرا رو چک کنه.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@dp.callback_query(F.data == "fj_add")
async def fj_add_callback(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer()
    _pending_forcejoin.add(callback.from_user.id)
    await callback.message.answer(
        "یه پیام از همون کانال رو <b>فوروارد کن</b> به اینجا (ساده‌ترین راه)، "
        "یا مستقیم @یوزرنیم کانال رو بفرست.\n\n"
        "⚠️ قبلش بات رو ادمین اون کانال کن، وگرنه نمی‌تونه عضویت کاربرا رو چک کنه.\n\n"
        "برای لغو: /cancel"
    )


@dp.callback_query(F.data.startswith("fj_remove:"))
async def fj_remove_callback(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    idx = int(callback.data.split(":", 1)[1])
    from db.session import async_session_maker
    async with async_session_maker() as session:
        channels = await _get_force_join_channels(session)
        if 0 <= idx < len(channels):
            removed = channels.pop(idx)
            await _save_force_join_channels(session, channels)
            await callback.answer(f"حذف شد: {removed.get('title') or removed['id']}")
        else:
            await callback.answer("پیدا نشد.")
            return
    try:
        await callback.message.delete()
    except Exception:
        pass


@dp.message(Command("selfdestruct"))
async def selfdestruct_panel_handler(message: types.Message):
    """
    پنل مدیریت «خودتخریب فایل» - فقط ادمین.
    برای دامنه‌های حساس (مثلاً سایت‌های نامناسب/بزرگسالان) که ادمین صریحاً
    اضافه کرده، فایل بعد از ارسال، بعد از یه مدت مشخص خودکار از چت پاک می‌شه.
    رو یوتیوب/اینستا/تیک‌تاک/آهنگ و بقیه‌ی سایت‌های عادی هیچ تاثیری نداره،
    چون فقط دامنه‌هایی که اینجا صریحاً اضافه بشن رو شامل می‌شه.
    """
    if not _is_admin(message.from_user.id):
        return

    from db.session import async_session_maker
    async with async_session_maker() as session:
        domains = await _get_self_destruct_domains(session)
        seconds = await _get_self_destruct_seconds(session)

    if domains:
        listing = "دامنه‌های فعلی:\n" + "\n".join(f"{i + 1}. <code>{html.escape(d)}</code>" for i, d in enumerate(domains))
    else:
        listing = "هنوز هیچ دامنه‌ای اضافه نشده (یعنی این قابلیت فعلاً برای هیچ سایتی فعال نیست)."

    rows = [
        [InlineKeyboardButton(text="➕ افزودن دامنه", callback_data="sd_add", style="success")],
        [InlineKeyboardButton(text=f"⏱ تغییر زمان (فعلی: {seconds} ثانیه)", callback_data="sd_time", style="primary")],
    ]
    for i, d in enumerate(domains):
        rows.append([InlineKeyboardButton(text=f"❌ حذف: {d}", callback_data=f"sd_remove:{i}", style="danger")])

    await message.answer(
        f"<b>🔥 خودتخریب فایل</b>\n\n"
        f"{listing}\n\n"
        f"⏱ زمان فعلی: <b>{seconds} ثانیه</b>\n\n"
        f"<i>فقط برای دامنه‌های بالا اعمال می‌شه؛ رو یوتیوب/اینستاگرام/تیک‌تاک/آهنگ و بقیه هیچ تاثیری نداره.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@dp.callback_query(F.data == "sd_add")
async def sd_add_callback(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer()
    _pending_selfdestruct[callback.from_user.id] = "await_domain"
    await callback.message.answer(
        "دامنه رو بدون http/www بفرست (مثلاً <code>example.com</code>).\n"
        "زیردامنه‌هاش هم خودکار شامل می‌شن.\n\n"
        "برای لغو: /cancel"
    )


@dp.callback_query(F.data == "sd_time")
async def sd_time_callback(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer()
    _pending_selfdestruct[callback.from_user.id] = "await_seconds"
    await callback.message.answer(
        "چند ثانیه بعد از ارسال، فایل پاک بشه؟ فقط عدد بفرست (مثلاً <code>90</code> برای ۱.۵ دقیقه).\n\n"
        "برای لغو: /cancel"
    )


@dp.callback_query(F.data.startswith("sd_remove:"))
async def sd_remove_callback(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    idx = int(callback.data.split(":", 1)[1])
    from db.session import async_session_maker
    from db.crud import BotConfigCRUD
    async with async_session_maker() as session:
        domains = await _get_self_destruct_domains(session)
        if 0 <= idx < len(domains):
            removed = domains.pop(idx)
            await BotConfigCRUD.set(session, "self_destruct_domains", json.dumps(domains))
            await callback.answer(f"حذف شد: {removed}")
        else:
            await callback.answer("پیدا نشد.")
            return
    try:
        await callback.message.delete()
    except Exception:
        pass



# admin_telegram_id -> در حال ویرایش کدوم متنه (فقط تو حافظه، نیازی به پایداری نداره)
_pending_text_edit: dict[int, str] = {}

# کاربرهایی که روی دکمه‌ی «پشتیبانی» زدن و منتظریم پیامشون رو بفرستن
_pending_support: set[int] = set()

# کاربرهایی که /set_thumb زدن (یا دکمه‌ش رو) و منتظریم عکس thumbnail رو بفرستن
_pending_thumb: set[int] = set()

# کاربرهایی که منتظریم شهر/timezone خودشون رو تایپ کنن
_pending_timezone: set[int] = set()

# ادمین‌هایی که /broadcast زدن و منتظریم متن پیام همگانی رو بفرستن
# مقدار: {"html_text": ...} - بعد از دریافت متن، منتظر انتخاب target می‌مونیم
_pending_broadcast: dict[int, dict] = {}

# (chat_id ادمین، message_id پیام فوروارد شده) -> telegram_id کاربر اصلی
# تا وقتی ادمین روی پیام پشتیبانی ریپلای می‌کنه، بدونیم باید جواب کدوم کاربر بره
_support_replies: dict[tuple[int, int], int] = {}

# ادمین در حال گرفتن (capture) ایموجی پرمیومه - فقط تو حافظه.
# state["stage"] == "await_emoji" یعنی منتظر پیامی هستیم که ایموجی(های)
# پرمیوم توشه؛ همون کاراکتر خودش کلید نگاشته می‌شه، نیاز به اسم دستی نیست.
_pending_emoji: dict[int, dict] = {}


def _extract_custom_emoji_ids(message) -> list[tuple[str, str]]:
    """همه‌ی ایموجی‌های پرمیوم داخل یه پیام رو برمی‌گردونه: [(کاراکتر, custom_emoji_id), ...]
    (می‌تونه چندتا با هم تو یه پیام باشه، هرکدوم جدا ثبت می‌شه)."""
    if not message.entities or not message.text:
        return []
    out = []
    for entity in message.entities:
        if entity.type == "custom_emoji" and getattr(entity, "custom_emoji_id", None):
            char = message.text[entity.offset: entity.offset + entity.length]
            out.append((char, entity.custom_emoji_id))
    return out


async def _get_force_join_channels(session) -> list:
    """لیست کانال‌های عضویت اجباری: [{"id": ..., "title": ..., "link": ...}, ...]"""
    from db.crud import BotConfigCRUD
    raw = await BotConfigCRUD.get(session, "force_join_channels")
    try:
        return json.loads(raw) if raw else []
    except Exception:
        return []


async def _save_force_join_channels(session, channels: list):
    from db.crud import BotConfigCRUD
    await BotConfigCRUD.set(session, "force_join_channels", json.dumps(channels))


# ادمین‌هایی که منتظریم برای عضویت اجباری یه کانال معرفی کنن (فوروارد یا @username)
_pending_forcejoin: set[int] = set()


def _parse_channel_input(text: str) -> Optional[str]:
    """از رو یه لینک t.me یا @username، شناسه‌ی قابل استفاده برای get_chat_member می‌سازه"""
    text = text.strip()
    m = re.match(r'(?:https?://)?t\.me/([a-zA-Z0-9_]+)/?$', text)
    if m:
        return f"@{m.group(1)}"
    if text.startswith("@") and re.match(r'^@[a-zA-Z0-9_]{4,}$', text):
        return text
    if re.match(r'^-?\d+$', text):
        return text
    return None


async def _get_self_destruct_domains(session) -> list:
    from db.crud import BotConfigCRUD
    raw = await BotConfigCRUD.get(session, "self_destruct_domains")
    try:
        return json.loads(raw) if raw else []
    except Exception:
        return []


async def _get_self_destruct_seconds(session) -> int:
    from db.crud import BotConfigCRUD
    raw = await BotConfigCRUD.get(session, "self_destruct_seconds")
    try:
        return int(raw) if raw else 90
    except Exception:
        return 90


# ادمین‌هایی که منتظریم یه دامنه/زمان برای خودتخریب فایل معرفی کنن.
# مقدار: "await_domain" یا "await_seconds"
_pending_selfdestruct: dict[int, str] = {}


async def _main_menu_keyboard(session) -> InlineKeyboardMarkup:
    buttons = {
        "menu_help": InlineKeyboardButton(
            text="📖 راهنما", callback_data="menu_help", style="primary",
        ),
        "menu_mystats": InlineKeyboardButton(
            text="📊 آمار من", callback_data="menu_mystats", style="primary",
        ),
        "menu_support": InlineKeyboardButton(
            text="🆘 پشتیبانی", callback_data="menu_support", style="success",
        ),
        "menu_settings": InlineKeyboardButton(
            text="⚙️ تنظیمات", callback_data="menu_settings",
        ),
    }
    return InlineKeyboardMarkup(inline_keyboard=[
        [buttons["menu_help"], buttons["menu_mystats"]],
        [buttons["menu_support"], buttons["menu_settings"]],
    ])


QUALITY_LABELS = {
    "best": "🏆 بهترین کیفیت",
    "1080p": "1080p",
    "720p": "720p",
    "480p": "480p",
    "audio": "🎵 فقط صدا",
    "video_note": "⭕️ پیام ویدیویی (گرد)",
}


def _quality_keyboard(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=QUALITY_LABELS["best"], callback_data=f"quality:{job_id}:best", style="primary")],
        [
            InlineKeyboardButton(text=QUALITY_LABELS["1080p"], callback_data=f"quality:{job_id}:1080p", style="primary"),
            InlineKeyboardButton(text=QUALITY_LABELS["720p"], callback_data=f"quality:{job_id}:720p", style="primary"),
        ],
        [
            InlineKeyboardButton(text=QUALITY_LABELS["480p"], callback_data=f"quality:{job_id}:480p", style="primary"),
            InlineKeyboardButton(text=QUALITY_LABELS["audio"], callback_data=f"quality:{job_id}:audio", style="success"),
        ],
        [InlineKeyboardButton(text=QUALITY_LABELS["video_note"], callback_data=f"quality:{job_id}:video_note", style="success")],
        [InlineKeyboardButton(text="❌ لغو", callback_data=f"cancel_dl:{job_id}", style="danger")],
    ])


_CAPTION_TITLE_MAX_CHARS = 80


def _cached_caption_header_and_body(cached) -> tuple[str, str]:
    """هدرِ بولد و بدنه‌ی caption رو برای فایلِ کش‌شده می‌سازه.

    سایت‌هایی مثل تیک‌تاک/پینترست عنوان کوتاه واقعی ندارن - فیلد title همون
    کپشن کامل پسته (می‌تونه صدها کاراکتر/پر از هشتگ باشه). قبلاً این متن کامل
    عیناً تو هدر بولد <b>...</b> می‌رفت. حالا هدر رو کوتاه می‌کنیم و مطمئن
    می‌شیم متن کامل جایی تو بدنه گم نشه (همون منطقی که worker/tasks.py برای
    ارسال معمولی/غیرکش‌شده داره)."""
    raw_title = (cached.title or "بدون عنوان").strip()
    if len(raw_title) > _CAPTION_TITLE_MAX_CHARS:
        short_title = raw_title[:_CAPTION_TITLE_MAX_CHARS].rstrip() + "…"
    else:
        short_title = raw_title
    safe_title = html.escape(short_title)

    description = (cached.description or "").strip()
    header_was_truncated = short_title != raw_title

    if description and description != raw_title:
        body = f"\n{html.escape(description)}\n"
    elif header_was_truncated:
        body = f"\n{html.escape(description or raw_title)}\n"
    else:
        body = ""
    return safe_title, body


async def _send_long_caption_remainder(sent_message, full_caption: str):
    """اگه caption واقعی از سقف ۱۰۲۴ کاراکتریِ تلگرام بلندتر بود و بالاتر برش
    خورد، ادامه‌ش رو گم نمی‌کنیم - جدا (ریپلای به همون فایل) می‌فرستیم. همون
    باگی که تو ارسال معمولی (worker/tasks.py) بود، اینجا (ارسال از کش) هم بود."""
    if not sent_message or not full_caption or len(full_caption) <= 1024:
        return
    remainder = full_caption[1010:].lstrip("… \n")
    if not remainder:
        return
    try:
        first = True
        for i in range(0, len(remainder), 4096):
            chunk = remainder[i:i + 4096]
            text = ("📝 <b>ادامه‌ی کپشن:</b>\n\n" + chunk) if first else chunk
            first = False
            await sent_message.reply(text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"ارسال ادامه‌ی کپشنِ بلند (کش) ناموفق بود: {e}")


async def _send_cached_file(target, cached):
    """ارسال فایل از کش - قابل استفاده هم برای پیام معمولی هم برای callback.message"""
    safe_title, body = _cached_caption_header_and_body(cached)
    if cached.file_type == FileType.AUDIO:
        full_caption = f"<b>🎵 {safe_title}</b>\n"
        full_caption += f"👤 {html.escape(cached.artist or 'هنرمند نامشخص')}\n"
        if cached.album:
            full_caption += f"💿 {html.escape(cached.album)}\n"
        if cached.duration:
            from datetime import timedelta
            full_caption += f"⏱ {timedelta(seconds=cached.duration)}\n"
        full_caption += body
        caption = full_caption
        if len(caption) > 1024:
            caption = caption[:1010].rstrip() + "…"
        sent = await target.answer_audio(cached.telegram_file_id, caption=caption, parse_mode="HTML")
        await _send_long_caption_remainder(sent, full_caption)
    elif cached.file_type == FileType.DOCUMENT:
        full_caption = f"<b>📦 {html.escape(cached.file_name or cached.title or 'فایل')}</b>\n"
        full_caption += body
        caption = full_caption
        if len(caption) > 1024:
            caption = caption[:1010].rstrip() + "…"
        sent = await target.answer_document(cached.telegram_file_id, caption=caption, parse_mode="HTML")
        await _send_long_caption_remainder(sent, full_caption)
    elif cached.file_type == FileType.PHOTO:
        full_caption = f"<b>🖼 {safe_title}</b>\n"
        full_caption += body
        caption = full_caption
        if len(caption) > 1024:
            caption = caption[:1010].rstrip() + "…"
        sent = await target.answer_photo(cached.telegram_file_id, caption=caption, parse_mode="HTML")
        await _send_long_caption_remainder(sent, full_caption)
    elif cached.file_type == FileType.VIDEO_NOTE:
        # پیام ویدیویی (گرد) توی تلگرام caption قبول نمی‌کنه
        await target.answer_video_note(cached.telegram_file_id)
    else:
        full_caption = f"<b>🎬 {safe_title}</b>\n"
        if cached.artist:
            full_caption += f"👤 {html.escape(cached.artist)}\n"
        if cached.duration:
            from datetime import timedelta
            full_caption += f"⏱ {timedelta(seconds=cached.duration)}\n"
        full_caption += body
        caption = full_caption
        if len(caption) > 1024:
            caption = caption[:1010].rstrip() + "…"
        sent = await target.answer_video(cached.telegram_file_id, caption=caption, parse_mode="HTML")
        await _send_long_caption_remainder(sent, full_caption)


@dp.error()
async def global_error_handler(event: types.ErrorEvent):
    """
    شبکه‌ی ایمنیِ آخر: اگه یه خطای پیش‌بینی‌نشده از هر هندلری بیرون بزنه
    (مثلاً وقتی چندین کاربر همزمان پیام می‌فرستن و یه جای غیرمنتظره خطا می‌ده)،
    این‌جا فقط لاگ می‌شه و کل بات و پردازش بقیه‌ی کاربرا متوقف نمی‌شه.
    """
    logger.error(f"خطای پیش‌بینی‌نشده تو یه هندلر: {event.exception}", exc_info=event.exception)
    return True


@dp.my_chat_member()
async def on_bot_membership_changed(event: types.ChatMemberUpdated):
    """وقتی بات به یه گروه/سوپرگروه اضافه می‌شه، خودشو معرفی می‌کنه و می‌گه
    برای کارکرد کامل (پاک کردن پیام وضعیت + ری‌اکشن) بهش دسترسی ادمین بدن."""
    if event.new_chat_member.user.id != bot.id:
        return

    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    just_joined = old_status in ("left", "kicked") and new_status in ("member", "administrator", "restricted")
    if not just_joined:
        return

    if event.chat.type not in ("group", "supergroup"):
        return

    from db.session import async_session_maker
    async with async_session_maker() as session:
        text = await get_text(session, "text_group_welcome")

    try:
        await bot.send_message(event.chat.id, text)
    except Exception as e:
        logger.warning(f"نتونستم پیام معرفی رو تو چت {event.chat.id} بفرستم: {e}")


@dp.message(CommandStart())
async def start_handler(message: types.Message):
    from db.session import async_session_maker
    async with async_session_maker() as session:
        user = await UserCRUD.get_or_create(
            session,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.first_name or "کاربر"
        )
        text = await get_text(session, "text_start", name=message.from_user.first_name or "دوست")
        keyboard = await _main_menu_keyboard(session)
    await message.answer(text, reply_markup=keyboard)


@dp.message(Command("help"))
async def help_handler(message: types.Message):
    from db.session import async_session_maker
    async with async_session_maker() as session:
        text = await get_text(session, "text_help")
    await message.answer(text)


@dp.callback_query(F.data == "menu_help")
async def menu_help_callback(callback: CallbackQuery):
    from db.session import async_session_maker
    await callback.answer()
    async with async_session_maker() as session:
        text = await get_text(session, "text_help")
    await callback.message.answer(text)


@dp.callback_query(F.data == "menu_support")
async def menu_support_callback(callback: CallbackQuery):
    from db.session import async_session_maker
    await callback.answer()
    _pending_support.add(callback.from_user.id)
    async with async_session_maker() as session:
        text = await get_text(session, "text_support")
    await callback.message.answer(text)


@dp.callback_query(F.data == "menu_mystats")
async def menu_mystats_callback(callback: CallbackQuery):
    from db.session import async_session_maker
    from db.crud import DownloadJobCRUD

    async with async_session_maker() as session:
        user = await UserCRUD.get_or_create(
            session,
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
            full_name=callback.from_user.first_name or "کاربر"
        )
        summary = await DownloadJobCRUD.get_user_summary(session, user.id)

    await callback.answer()
    await callback.message.answer(
        f"<b>📊 آمار تو</b>\n\n"
        f"📥 کل دانلودها: {summary['total']}\n"
        f"✅ موفق: {summary['success']}\n"
        f"📅 امروز: {summary['today']} از {settings.MAX_DAILY_DOWNLOADS}"
    )


# ============================================================
# ⚙️ منوی تنظیمات شخصی کاربر (/settings)
# ============================================================

def _settings_keyboard(user) -> InlineKeyboardMarkup:
    spoiler_on = bool(getattr(user, "spoiler_effect", False))
    doc_on = bool(getattr(user, "upload_as_document", False))
    rename_on = bool(getattr(user, "show_rename_option", False))
    screenshots_on = getattr(user, "receive_screenshots", None)
    screenshots_on = True if screenshots_on is None else bool(screenshots_on)  # پیش‌فرض روشن
    tz = getattr(user, "timezone", None) or "تنظیم‌نشده"
    rows = [
        [InlineKeyboardButton(text=f"⏰ تنظیم منطقه‌ی زمانی ({tz})", callback_data="set_tz")],
        [InlineKeyboardButton(text="🖼 دیدن Thumbnail فعلی", callback_data="thumb_view")],
        [InlineKeyboardButton(text="🗑 حذف Thumbnail", callback_data="thumb_delete")],
        [InlineKeyboardButton(
            text=f"💥 افکت اسپویلر: {'روشن ✅' if spoiler_on else 'خاموش ❌'}",
            callback_data="toggle_spoiler",
        )],
        [InlineKeyboardButton(
            text=f"📎 ارسال به‌صورت فایل/سند: {'روشن ✅' if doc_on else 'خاموش ❌'}",
            callback_data="toggle_docmode",
        )],
        [InlineKeyboardButton(
            text=f"✏️ نمایش گزینه‌ی تغییر نام: {'روشن ✅' if rename_on else 'خاموش ❌'}",
            callback_data="toggle_rename",
        )],
        [InlineKeyboardButton(
            text=f"📸 دریافت اسکرین‌شات پیش‌نمایش: {'روشن ✅' if screenshots_on else 'خاموش ❌'}",
            callback_data="toggle_screenshots",
        )],
        [InlineKeyboardButton(text="« بازگشت", callback_data="menu_back")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _get_settings_user(telegram_id: int, username: str, full_name: str):
    from db.session import async_session_maker
    async with async_session_maker() as session:
        return await UserCRUD.get_or_create(
            session, telegram_id=telegram_id, username=username, full_name=full_name or "کاربر"
        )


@dp.message(Command("settings"))
async def settings_handler(message: types.Message):
    user = await _get_settings_user(
        message.from_user.id, message.from_user.username, message.from_user.first_name
    )
    await message.answer(
        "⚙️ <b>تنظیمات شخصی تو</b>\n\n"
        "می‌تونی برای فایل‌هایی که دانلود می‌کنی یه thumbnail دلخواه ست کنی "
        "(کافیه یه عکس بفرستی، لازم نیست هر بار دستور بزنی)، افکت اسپویلر رو "
        "روشن/خاموش کنی، یا انتخاب کنی همیشه به‌صورت فایل/سند برات بفرستم.",
        reply_markup=_settings_keyboard(user),
    )


@dp.callback_query(F.data == "menu_settings")
async def menu_settings_callback(callback: CallbackQuery):
    user = await _get_settings_user(
        callback.from_user.id, callback.from_user.username, callback.from_user.first_name
    )
    await callback.answer()
    await callback.message.answer(
        "⚙️ <b>تنظیمات شخصی تو</b>",
        reply_markup=_settings_keyboard(user),
    )


@dp.callback_query(F.data == "menu_back")
async def settings_back_callback(callback: CallbackQuery):
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass


_SETTINGS_TOGGLE_FIELDS = {
    "toggle_spoiler": "spoiler_effect",
    "toggle_docmode": "upload_as_document",
    "toggle_rename": "show_rename_option",
    "toggle_screenshots": "receive_screenshots",
}


@dp.callback_query(F.data.in_(set(_SETTINGS_TOGGLE_FIELDS.keys())))
async def settings_toggle_callback(callback: CallbackQuery):
    from db.session import async_session_maker

    field = _SETTINGS_TOGGLE_FIELDS[callback.data]
    async with async_session_maker() as session:
        user = await UserCRUD.get_or_create(
            session,
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
            full_name=callback.from_user.first_name or "کاربر",
        )
        current = getattr(user, field, False)
        # receive_screenshots پیش‌فرضش (None) یعنی روشنه؛ بقیه پیش‌فرضشون خاموشه
        current_bool = True if (field == "receive_screenshots" and current is None) else bool(current)
        new_value = not current_bool
        user = await UserCRUD.update_settings(session, callback.from_user.id, **{field: new_value})

    await callback.answer("✅ ذخیره شد")
    try:
        await callback.message.edit_reply_markup(reply_markup=_settings_keyboard(user))
    except Exception:
        pass


@dp.callback_query(F.data == "thumb_view")
async def thumb_view_callback(callback: CallbackQuery):
    from db.session import async_session_maker

    async with async_session_maker() as session:
        user = await UserCRUD.get_or_create(
            session,
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
            full_name=callback.from_user.first_name or "کاربر",
        )
    await callback.answer()
    if user.custom_thumbnail_file_id:
        await callback.message.answer_photo(
            user.custom_thumbnail_file_id, caption="🖼 این thumbnail فعلی توئه."
        )
    else:
        await callback.message.answer(
            "هنوز thumbnail‌ای ست نکردی. فقط کافیه یه عکس برام بفرستی 📷"
        )


@dp.callback_query(F.data == "thumb_delete")
async def thumb_delete_callback(callback: CallbackQuery):
    from db.session import async_session_maker

    async with async_session_maker() as session:
        await UserCRUD.update_settings(
            session, callback.from_user.id, custom_thumbnail_file_id=None
        )
    await callback.answer("🗑 حذف شد")
    await callback.message.answer("Thumbnail حذف شد.")


@dp.callback_query(F.data == "set_tz")
async def set_tz_callback(callback: CallbackQuery):
    await callback.answer()
    _pending_timezone.add(callback.from_user.id)
    await callback.message.answer(
        "⏰ اسم شهرت رو به فارسی یا انگلیسی بفرست (مثلاً «تهران» یا «Tehran»)، "
        "یا یه لوکیشن تلگرامی برام شیر کن."
    )


@dp.message(Command("set_thumb"))
async def set_thumb_handler(message: types.Message):
    _pending_thumb.add(message.from_user.id)
    await message.answer(
        "لطفاً عکسی که می‌خوای به‌عنوان thumbnail دلخواه ست بشه رو بفرست.\n\n"
        "لازم نیست هر بار این دستور رو بزنی، بعد از این هر عکسی که بفرستی همینو "
        "به‌عنوان thumbnail استفاده می‌کنم 😅"
    )


# نگاشت اسم فارسی/انگلیسی شهرهای پرکاربرد به IANA timezone - چون کاربرها معمولاً
# اسم شهر می‌فرستن نه خودِ رشته‌ی فنی «Asia/Tehran»
_CITY_TZ_MAP = {
    "تهران": "Asia/Tehran", "tehran": "Asia/Tehran", "iran": "Asia/Tehran",
    "دبی": "Asia/Dubai", "dubai": "Asia/Dubai",
    "استانبول": "Europe/Istanbul", "istanbul": "Europe/Istanbul", "turkey": "Europe/Istanbul",
    "لندن": "Europe/London", "london": "Europe/London",
    "برلین": "Europe/Berlin", "berlin": "Europe/Berlin", "germany": "Europe/Berlin",
    "نیویورک": "America/New_York", "new york": "America/New_York",
}


async def _handle_pending_settings_text(message: types.Message) -> bool:
    """اگه کاربر تو حالت انتظار (thumbnail/timezone) بود، متنش رو پردازش می‌کنه.
    True برمی‌گردونه اگه پیام رو مصرف کرده باشه (پس دیگه نباید به‌عنوان لینک دانلود پردازش بشه)."""
    uid = message.from_user.id

    if uid in _pending_timezone and message.text:
        _pending_timezone.discard(uid)
        from db.session import async_session_maker
        key = message.text.strip().lower()
        tz = _CITY_TZ_MAP.get(key) or _CITY_TZ_MAP.get(message.text.strip())
        if not tz:
            await message.answer(
                "این شهر رو نشناختم 🙁 فعلاً از این‌ها یکی رو امتحان کن: "
                "تهران، دبی، استانبول، لندن، برلین، نیویورک."
            )
            return True
        async with async_session_maker() as session:
            await UserCRUD.update_settings(session, uid, timezone=tz)
        await message.answer(f"✅ منطقه‌ی زمانی روی «{tz}» تنظیم شد.")
        return True

    return False


@dp.message(F.location)
async def handle_location_for_timezone(message: types.Message):
    uid = message.from_user.id
    if uid not in _pending_timezone:
        # aiogram بعد از اولین هندلرِ match‌شده propagation رو متوقف می‌کنه
        # (return ساده کافیه تا آپدیت «مصرف‌شده» حساب بشه) - پس بی‌SkipHandler
        # همه‌ی لوکیشن‌ها همین‌جا گم می‌شدن.
        raise SkipHandler
    _pending_timezone.discard(uid)
    # بدون سرویس جغرافیایی خارجی، از روی longitude یه تخمین ساده UTC offset می‌زنیم
    # و به نزدیک‌ترین timezone شناخته‌شده گرد می‌کنیم - برای دقت کامل بعداً می‌شه
    # timezonefinder اضافه کرد.
    lon = message.location.longitude
    from db.session import async_session_maker
    tz = "Asia/Tehran" if 44 <= lon <= 64 else "Etc/UTC"
    async with async_session_maker() as session:
        await UserCRUD.update_settings(session, uid, timezone=tz)
    await message.answer(f"✅ منطقه‌ی زمانی روی «{tz}» تنظیم شد.")


@dp.message(F.photo)
async def handle_thumbnail_photo(message: types.Message):
    # نکته‌ی مهم: چون این بات از قبل عکس‌های ارسالی تو چت خصوصی رو برای فیچر
    # «دانلود مستقیم فایلِ ارسالی» مصرف می‌کنه (نگاه کن به handle_other/
    # _prompt_upload_direct_link)، عمداً *فقط* وقتی uid تو _pending_thumb هست
    # (یعنی /set_thumb زده یا دکمه‌ی مربوطه رو زده) عکس رو به‌عنوان thumbnail
    # می‌گیریم - نه هر عکسی که فرستاده می‌شه. اگه می‌خوای رفتار «هر عکسی خودکار
    # thumbnail بشه» (مثل اسکرین‌شاتی که فرستادی) رو هم بخوای، به‌جاش این چک
    # رو بردار.
    uid = message.from_user.id
    if uid not in _pending_thumb:
        # ⚠️ بدون SkipHandler این هندلر *همه‌ی* عکس‌ها رو می‌بلعید و عکس هرگز
        # به handle_other (فیچر «فایل آپلودی ← لینک مستقیم») نمی‌رسید - aiogram
        # بعد از اولین هندلرِ match‌شده propagation رو متوقف می‌کنه، حتی اگه
        # هندلر فقط return کنه. raise کردن SkipHandler یعنی «مصرفش نکردم،
        # بده به هندلر بعدی».
        raise SkipHandler
    _pending_thumb.discard(uid)
    from db.session import async_session_maker
    file_id = message.photo[-1].file_id
    async with async_session_maker() as session:
        await UserCRUD.update_settings(session, uid, custom_thumbnail_file_id=file_id)
    await message.answer("✅ Thumbnail ذخیره شد و از این به بعد برای فایل‌هات استفاده می‌شه.")


def _is_admin(user_id: int) -> bool:
    """آیا این کاربر ادمین بات هست؟"""
    return user_id in settings.ADMIN_IDS


@dp.message(Command("stats"))
async def stats_handler(message: types.Message):
    """آمار بات - فقط برای ادمین"""
    if not _is_admin(message.from_user.id):
        return

    from db.session import async_session_maker
    from db.crud import DownloadStatsCRUD, DownloadJobCRUD, BotConfigCRUD

    async with async_session_maker() as session:
        stats = await DownloadStatsCRUD.get_today_stats(session)
        enabled = await BotConfigCRUD.get_bool(session, "bot_enabled", True)
        max_size = await BotConfigCRUD.get_int(session, "max_file_size_mb", settings.MAX_FILE_SIZE_MB)
        # «پرکاربردترین» رو از خود jobهای امروز حساب می‌کنیم (mode واقعی) -
        # ستون most_used_site قبلاً با هر دانلود overwrite می‌شد و عملاً
        # «آخرین سایت» بود، نه پرکاربردترین.
        top_site = await DownloadJobCRUD.get_most_used_site_today(session)

    status_emoji = "🟢 روشن" if enabled else "🔴 خاموش"
    if stats:
        await message.answer(
            f"<b>📊 آمار امروز</b>\n\n"
            f"وضعیت بات: {status_emoji}\n"
            f"سقف حجم فایل: {max_size} MB\n\n"
            f"📥 کل دانلودها: {stats.total_downloads}\n"
            f"✅ موفق: {stats.successful_downloads}\n"
            f"❌ ناموفق: {stats.failed_downloads}\n"
            f"💾 حجم کل: {stats.total_size_mb:.1f} MB\n"
            f"🔥 پرکاربردترین سایت: {html.escape(top_site or '-')}"
        )
    else:
        await message.answer(
            f"<b>📊 آمار امروز</b>\n\n"
            f"وضعیت بات: {status_emoji}\n"
            f"سقف حجم فایل: {max_size} MB\n\n"
            f"هنوز امروز دانلودی ثبت نشده."
        )


@dp.callback_query(F.data.startswith("bcast_send:"))
async def broadcast_send_callback(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return

    target = callback.data.split(":", 1)[1]
    pending_b = _pending_broadcast.pop(callback.from_user.id, None)

    if target == "cancel" or not pending_b or "html_text" not in pending_b:
        await callback.answer()
        await callback.message.edit_text("لغو شد.")
        return

    await callback.answer("شروع شد...")
    html_text = pending_b["html_text"]

    from db.session import async_session_maker
    async with async_session_maker() as session:
        if target == "active7":
            targets = await UserCRUD.get_active_telegram_ids_since(session, days=7)
        else:
            targets = await UserCRUD.get_all_telegram_ids(session)

    await callback.message.edit_text(f"📤 در حال ارسال به {len(targets)} کاربر...")

    sent, failed = 0, 0
    for i, telegram_id in enumerate(targets):
        try:
            await bot.send_message(telegram_id, html_text, parse_mode="HTML")
            sent += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.send_message(telegram_id, html_text, parse_mode="HTML")
                sent += 1
            except Exception:
                failed += 1
        except Exception:
            # کاربر بات رو بلاک کرده، اکانتش پاک شده و غیره - طبیعیه، فقط می‌شمریم
            failed += 1

        # فاصله‌ی کوچیک بین پیام‌ها تا محدودیت نرخ تلگرام (~30 پیام/ثانیه به چت‌های
        # مختلف) رو رد نکنیم؛ هر ۵۰ تا هم پیشرفت رو آپدیت می‌کنیم که ادمین بدونه گیر نکرده
        await asyncio.sleep(0.05)
        if (i + 1) % 50 == 0:
            try:
                await callback.message.edit_text(f"📤 در حال ارسال... {i + 1}/{len(targets)}")
            except Exception:
                pass

    await callback.message.edit_text(
        f"✅ پیام همگانی تموم شد.\n\n📬 موفق: {sent}\n🚫 ناموفق (بلاک/حذف‌شده): {failed}"
    )


@dp.message(Command("cancel"))
async def cancel_pending_handler(message: types.Message):
    """لغو هر حالت انتظاریِ در جریان.

    قبلاً فقط چندتا از stateها رو پاک می‌کرد؛ بقیه (emoji/forcejoin/
    selfdestruct/text_edit/support/upload-name) با اینکه پیام راهنماشون
    می‌گفت «برای لغو: /cancel» واقعاً لغو نمی‌شدن — چون چک‌های داخلی
    «if text == "/cancel"» تو handle_message هرگز اجرا نمی‌شدن (هندلر
    Command("cancel") اول match می‌شه و جلوی بقیه رو می‌گیره). وخیم‌ترین
    حالت text_edit بود: /cancel کار نمی‌کرد و پیام بعدیِ ادمین جایگزین
    یکی از متن‌های ربات می‌شد."""
    uid = message.from_user.id
    had_pending = (
        uid in _pending_broadcast
        or uid in _pending_thumb
        or uid in _pending_timezone
        or uid in _pending_direct_link_name
        or uid in _pending_upload_direct_link_name
        or uid in _pending_emoji
        or uid in _pending_forcejoin
        or uid in _pending_selfdestruct
        or uid in _pending_text_edit
        or uid in _pending_support
    )
    _pending_broadcast.pop(uid, None)
    _pending_thumb.discard(uid)
    _pending_timezone.discard(uid)
    _pending_direct_link_name.pop(uid, None)
    _pending_upload_direct_link_name.pop(uid, None)
    _pending_emoji.pop(uid, None)
    _pending_forcejoin.discard(uid)
    _pending_selfdestruct.pop(uid, None)
    _pending_text_edit.pop(uid, None)
    _pending_support.discard(uid)
    await message.answer("لغو شد." if had_pending else "چیزی برای لغو کردن نبود.")


@dp.message(Command("broadcast"))
async def broadcast_handler(message: types.Message):
    """پیام همگانی به کاربرها - فقط برای ادمین"""
    if not _is_admin(message.from_user.id):
        return
    _pending_broadcast[message.from_user.id] = {"stage": "await_text"}
    await message.answer(
        "📢 متن پیام همگانی رو بفرست (می‌تونی از فرمت‌بندی تلگرام هم استفاده کنی - "
        "بولد، لینک و غیره).\n\nبرای انصراف: /cancel"
    )


@dp.message(Command("topsites"))
async def topsites_handler(message: types.Message):
    """پنل آماری پیشرفته‌تر - پرکاربردترین سایت‌ها (همه‌ی زمان‌ها) + دامنه‌هایی
    که detected_site براشون unknown شده (برای فهمیدن چی باید به LinkDetector
    اضافه بشه تا کمتر بگه unknown) - فقط برای ادمین"""
    if not _is_admin(message.from_user.id):
        return

    from db.session import async_session_maker
    from db.crud import DownloadJobCRUD

    async with async_session_maker() as session:
        sites = await DownloadJobCRUD.get_site_breakdown(session, limit=15)
        unknown_domains = await DownloadJobCRUD.get_top_unknown_domains(session, limit=10)

    text = "<b>📊 پرکاربردترین سایت‌ها (کل تاریخچه)</b>\n\n"
    if sites:
        for site_name, count in sites:
            text += f"• <code>{html.escape(site_name)}</code> — {count}\n"
    else:
        text += "هنوز دانلودی ثبت نشده.\n"

    text += "\n<b>❓ دامنه‌هایی که «unknown» تشخیص داده شدن</b>\n"
    text += "(اگه یه دامنه تکراری زیاد اومده، احتمالاً وقتشه پشتیبانیش به LinkDetector اضافه بشه)\n\n"
    if unknown_domains:
        for domain, count in unknown_domains:
            text += f"• <code>{html.escape(domain or '-')}</code> — {count} بار\n"
    else:
        text += "چیزی ثبت نشده 🎉 (یا این‌که آپدیت raw_domain هنوز اجرا نشده)\n"

    await message.answer(text)


@dp.message(Command("userinfo"))
async def userinfo_handler(message: types.Message):
    """اطلاعات و breakdown سایت‌های یه کاربر خاص - فقط برای ادمین.
    استفاده: یا ریپلای روی پیامِ فوروارد شده‌ی همون کاربر (مثل بخش پشتیبانی)،
    یا /userinfo <telegram_id>"""
    if not _is_admin(message.from_user.id):
        return

    from db.session import async_session_maker
    from db.crud import DownloadJobCRUD, DownloadJobCRUD as _JobCRUD

    target_id = None
    args = message.text.split(maxsplit=1)
    if len(args) > 1 and args[1].strip().lstrip("-").isdigit():
        target_id = int(args[1].strip())
    elif message.reply_to_message and message.reply_to_message.forward_from:
        target_id = message.reply_to_message.forward_from.id

    if not target_id:
        await message.answer(
            "یه آیدی عددی بده: <code>/userinfo 123456789</code>\n"
            "یا روی پیامِ فوروارد شده‌ی یه کاربر (مثلاً از بخش پشتیبانی) ریپلای کن."
        )
        return

    async with async_session_maker() as session:
        user = await UserCRUD.get_by_id(session, target_id)
        if not user:
            await message.answer("این کاربر تو دیتابیس پیدا نشد (شاید هنوز هیچ دانلودی نزده).")
            return
        summary = await DownloadJobCRUD.get_user_summary(session, user.id)
        site_breakdown = await DownloadJobCRUD.get_user_site_breakdown(session, user.id, limit=15)

    text = (
        f"<b>👤 اطلاعات کاربر</b>\n\n"
        f"آیدی: <code>{target_id}</code>\n"
        f"یوزرنیم: @{user.username if user.username else '-'}\n"
        f"اسم: {html.escape(user.full_name or '-')}\n\n"
        f"📥 کل دانلودها: {summary['total']}\n"
        f"✅ موفق: {summary['success']}\n"
        f"📅 امروز: {summary['today']}\n\n"
        f"<b>🌐 سایت‌هایی که استفاده کرده:</b>\n"
    )
    if site_breakdown:
        for site_name, count in site_breakdown:
            text += f"• <code>{html.escape(site_name)}</code> — {count}\n"
    else:
        text += "هنوز چیزی دانلود نکرده.\n"

    await message.answer(text)


@dp.message(Command("toggle"))
async def toggle_handler(message: types.Message):
    """روشن/خاموش کردن کل بات - فقط برای ادمین"""
    if not _is_admin(message.from_user.id):
        return

    from db.session import async_session_maker
    from db.crud import BotConfigCRUD

    async with async_session_maker() as session:
        enabled = await BotConfigCRUD.get_bool(session, "bot_enabled", True)
        new_value = not enabled
        await BotConfigCRUD.set(session, "bot_enabled", "true" if new_value else "false")

    await message.answer(
        "🟢 بات روشن شد، همه می‌تونن ازش استفاده کنن." if new_value
        else "🔴 بات خاموش شد، فعلاً هیچ‌کس (جز ادمین) نمی‌تونه استفاده کنه."
    )


@dp.message(Command("maxsize"))
async def maxsize_handler(message: types.Message):
    """تغییر سقف حجم فایل مجاز (مگابایت) - فقط برای ادمین"""
    if not _is_admin(message.from_user.id):
        return

    from db.session import async_session_maker
    from db.crud import BotConfigCRUD

    parts = message.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("فرمت درست: <code>/maxsize 500</code> (عدد به مگابایت)")
        return

    new_size = int(parts[1])
    async with async_session_maker() as session:
        await BotConfigCRUD.set(session, "max_file_size_mb", str(new_size))

    await message.answer(f"✅ سقف حجم فایل روی {new_size} مگابایت تنظیم شد.")


@dp.message(Command("texts"))
async def texts_handler(message: types.Message):
    """لیست متن‌های قابل ویرایش - فقط برای ادمین"""
    if not _is_admin(message.from_user.id):
        return

    rows = [
        [InlineKeyboardButton(text=info["label"], callback_data=f"edittext:{key}", style="primary")]
        for key, info in EDITABLE_TEXTS.items()
    ]
    await message.answer(
        "<b>✏️ ویرایش متن‌های بات</b>\n\n"
        "روی هر کدوم بزن تا متنش رو عوض کنی.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@dp.callback_query(F.data.startswith("edittext:"))
async def edittext_callback(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return

    key = callback.data.split(":", 1)[1]
    info = EDITABLE_TEXTS.get(key)
    if not info:
        await callback.answer("پیدا نشد.", show_alert=True)
        return

    from db.session import async_session_maker
    async with async_session_maker() as session:
        current = await get_text(session, key)

    _pending_text_edit[callback.from_user.id] = key
    await callback.answer()

    hint = f"\n\n💡 {info['hint']}" if info.get("hint") else ""
    await callback.message.answer(
        f"<b>{info['label']}</b>\n\n"
        f"متن فعلی:\n<code>{html.escape(current)}</code>{hint}\n\n"
        f"متن جدید رو همینجا بفرست.\n"
        f"برای لغو: /cancel\n"
        f"برای برگردوندن به پیش‌فرض: /reset"
    )


def _premium_emoji_panel_content(active: bool, registered: dict) -> tuple[str, InlineKeyboardMarkup]:
    status_line = "✅ روشن (فقط آیکن پریمیوم نشون داده می‌شه)" if active else "❌ خاموش (فقط ایموجی یونیکد معمولی)"
    lines = [
        "<b>🎨 ایموجی‌های پرمیوم</b>\n",
        f"وضعیت: {status_line}\n",
        "هر ایموجی که اینجا ثبت کنی، همه‌جای ربات (متن‌ها + دکمه‌ها) که همون "
        "کاراکتر توشه، خودکار پریمیوم می‌شه — نیازی به تغییر متن‌ها نیست.\n",
        "⚠️ فقط وقتی کار می‌کنه که خودِ اکانتی که ربات رو ساخته پریمیوم داشته باشه.\n",
    ]
    rows = []
    if registered:
        for idx, (char, emoji_id) in enumerate(registered.items()):
            lines.append(f"{char}  →  ثبت‌شده (…{emoji_id[-6:]})")
            rows.append([InlineKeyboardButton(text=f"🗑 حذف {char}", callback_data=f"delemoji:{idx}", style="danger")])
    else:
        lines.append("هنوز هیچ ایموجی‌ای ثبت نشده.")
    toggle_label = "❌ خاموش کردن ایموجی پریمیوم" if active else "✅ روشن کردن ایموجی پریمیوم"
    rows.append([InlineKeyboardButton(text=toggle_label, callback_data="emoji_toggle", style="danger" if active else "success")])
    rows.append([InlineKeyboardButton(text="➕ افزودن ایموجی جدید", callback_data="emoji_add", style="success")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("emoji"))
async def emoji_panel_handler(message: types.Message):
    """پنل مدیریت ایموجی پرمیوم - فقط برای ادمین (نیاز به پرمیوم روی اکانت بات‌دار داره)"""
    if not _is_admin(message.from_user.id):
        return
    active = await is_premium_active()
    registered = await get_registered_emojis()
    text, markup = _premium_emoji_panel_content(active, registered)
    await message.answer(text, reply_markup=markup)


async def _render_premium_emoji_panel(callback: CallbackQuery) -> None:
    active = await is_premium_active()
    registered = await get_registered_emojis()
    text, markup = _premium_emoji_panel_content(active, registered)
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception:
        await callback.message.answer(text, reply_markup=markup)


@dp.callback_query(F.data == "emoji_toggle")
async def emoji_toggle_callback(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    new_val = not await is_premium_active()
    await set_premium_active(new_val)
    await callback.answer(
        "💎 ایموجی پریمیوم روشن شد." if new_val else "ایموجی پریمیوم خاموش شد.",
        show_alert=True,
    )
    await _render_premium_emoji_panel(callback)


@dp.callback_query(F.data == "emoji_add")
async def emoji_add_callback(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer()
    _pending_emoji[callback.from_user.id] = {"stage": "await_emoji"}
    await callback.message.answer(
        "همون ایموجی پرمیوم موردنظرت رو مستقیم همین‌جا بفرست "
        "(باید خودِ ایموجی پرمیوم باشه، نه یه ایموجی معمولی؛ می‌تونی چندتا رو "
        "پشت‌سرهم هم بفرستی، هرکدوم رو جدا ثبت می‌کنم).\n\n"
        "برای لغو: /cancel"
    )


@dp.callback_query(F.data.startswith("delemoji:"))
async def emoji_delete_callback(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    try:
        idx = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("نامعتبر", show_alert=True)
        return
    items = list((await get_registered_emojis()).items())
    if not (0 <= idx < len(items)):
        await callback.answer("قبلاً حذف شده.", show_alert=True)
        await _render_premium_emoji_panel(callback)
        return
    char = items[idx][0]
    await unregister_emoji(char)
    await callback.answer(f"{char} پاک شد.", show_alert=True)
    await _render_premium_emoji_panel(callback)



@dp.message(F.text)
async def handle_message(message: types.Message):
    from db.session import async_session_maker
    from db.crud import DownloadJobCRUD, BotConfigCRUD
    from db.models import FileType, DownloadStatus

    # بات برای همه بازه، مگه این‌که ادمین با /toggle خاموش کرده باشه
    async with async_session_maker() as toggle_session:
        bot_enabled = await BotConfigCRUD.get_bool(toggle_session, "bot_enabled", True)
        # نگهبان دیسک (تسک check_disk_space در ورکر) موقع بحران فضای دیسک
        # این کلید رو critical می‌کنه - دانلود جدید یعنی چند گیگ فایل موقت
        # بیشتر روی دیسکی که از قبل پر است.
        disk_state = await BotConfigCRUD.get(toggle_session, "disk_space_state") or "ok"

    if not bot_enabled and not _is_admin(message.from_user.id):
        return

    if disk_state == "critical" and not _is_admin(message.from_user.id):
        await message.reply(
            "💾 فضای ذخیره‌سازی سرور تقریباً پر شده و دانلودهای جدید موقتاً بسته‌ست.\n"
            "لطفاً چند ساعت دیگه دوباره امتحان کن."
        )
        return

    text = message.text.strip()

    # اگه کاربر منتظر تایپ کردن شهر/timezone بود (از منوی تنظیمات)
    if message.from_user.id in _pending_timezone:
        if await _handle_pending_settings_text(message):
            return

    # اگه ادمین /broadcast زده و منتظریم متن پیام همگانی رو بفرسته
    if _is_admin(message.from_user.id) and message.from_user.id in _pending_broadcast:
        pending_b = _pending_broadcast[message.from_user.id]
        if pending_b.get("stage") == "await_text":
            _pending_broadcast[message.from_user.id] = {
                "stage": "confirm",
                "html_text": message.html_text or message.text,
            }
            preview_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📤 به همه بفرست", callback_data="bcast_send:all", style="primary")],
                [InlineKeyboardButton(text="📤 فقط فعال‌های ۷ روز اخیر", callback_data="bcast_send:active7", style="primary")],
                [InlineKeyboardButton(text="❌ لغو", callback_data="bcast_send:cancel", style="danger")],
            ])
            await message.answer(
                "پیش‌نمایش پیام:\n\n" + (message.html_text or html.escape(message.text)) +
                "\n\n⬆️ این پیام دقیقاً همینطوری برای کاربرها فرستاده می‌شه. برای کیا بفرستم؟",
                reply_markup=preview_keyboard
            )
            return

    # اگه ادمین داره به یه پیام پشتیبانیِ فوروارد شده ریپلای می‌کنه
    if _is_admin(message.from_user.id) and message.reply_to_message:
        reply_key = (message.chat.id, message.reply_to_message.message_id)
        target_user_id = _support_replies.get(reply_key)
        if target_user_id:
            try:
                await bot.send_message(
                    target_user_id,
                    f"💬 <b>پاسخ پشتیبانی:</b>\n\n{html.escape(message.text)}",
                )
                await message.reply("✅ پاسخ برای کاربر ارسال شد.")
            except Exception as e:
                logger.warning(f"ارسال پاسخ پشتیبانی به کاربر {target_user_id} ناموفق بود: {e}")
                await message.reply("⚠️ ارسال پاسخ ناموفق بود (شاید کاربر بات رو بلاک کرده).")
            return

    # اگه ادمین در حال گرفتن (capture) یه ایموجی پرمیومه (از /emoji)
    if _is_admin(message.from_user.id) and message.from_user.id in _pending_emoji:
        state = _pending_emoji[message.from_user.id]

        if text == "/cancel":
            del _pending_emoji[message.from_user.id]
            await message.answer("لغو شد.")
            return

        if state["stage"] == "await_emoji":
            pairs = _extract_custom_emoji_ids(message)
            if not pairs:
                await message.answer(
                    "تو این پیام هیچ ایموجی پرمیومی پیدا نکردم. یه پیام بفرست که واقعاً "
                    "توش یه ایموجی پرمیوم باشه (از پنل ایموجی خودِ تلگرام انتخابش کن)، یا /cancel برای پایان."
                )
                return
            saved = []
            for char, emoji_id in pairs:
                await register_emoji(char, emoji_id)
                saved.append(char)
            await message.answer(
                f"✅ ثبت شد: {' '.join(saved)}\n"
                "می‌تونی ایموجی بعدی رو هم بفرستی، یا /cancel برای پایان."
            )
            return

    # اگه ادمین در حال معرفی یه کانال جدید برای عضویت اجباریه
    if _is_admin(message.from_user.id) and message.from_user.id in _pending_forcejoin:
        if text == "/cancel":
            _pending_forcejoin.discard(message.from_user.id)
            await message.answer("لغو شد.")
            return

        channel_id = None
        channel_title = None
        channel_username = None

        origin = message.forward_origin
        if origin and getattr(origin, "type", None) == "channel":
            channel_id = origin.chat.id
            channel_title = origin.chat.title
            channel_username = origin.chat.username
        else:
            parsed = _parse_channel_input(text)
            if parsed:
                channel_id = parsed

        if channel_id is None:
            await message.answer(
                "این رو نفهمیدم. یه پیام از کانال فوروارد کن یا @username کانال رو بفرست.\n"
                "برای لغو: /cancel"
            )
            return

        # چک کنیم بات خودش تو اون کانال ادمینه، وگرنه چک عضویت کاربرا اصلاً کار نمی‌کنه
        try:
            bot_member = await bot.get_chat_member(channel_id, bot.id)
            if bot_member.status not in ("administrator", "creator"):
                raise RuntimeError("بات تو این کانال ادمین نیست")
            chat_info = await bot.get_chat(channel_id)
            channel_title = channel_title or chat_info.title
            channel_username = channel_username or chat_info.username
            channel_id = chat_info.id
        except Exception as e:
            _pending_forcejoin.discard(message.from_user.id)
            await message.answer(
                f"⚠️ نتونستم این کانال رو تایید کنم: {html.escape(str(e))}\n"
                "مطمئن شو بات رو ادمین کانال کردی، بعد دوباره از /forcejoin امتحان کن."
            )
            return

        link = f"https://t.me/{channel_username}" if channel_username else None
        if not link:
            try:
                invite = await bot.create_chat_invite_link(channel_id)
                link = invite.invite_link
            except Exception as e:
                logger.warning(f"ساخت لینک دعوت برای کانال {channel_id} ناموفق بود: {e}")

        async with async_session_maker() as session:
            channels = await _get_force_join_channels(session)
            channels.append({"id": str(channel_id), "title": channel_title or str(channel_id), "link": link})
            await _save_force_join_channels(session, channels)

        _pending_forcejoin.discard(message.from_user.id)
        await message.answer(f"✅ کانال «{channel_title or channel_id}» اضافه شد به لیست عضویت اجباری.")
        return

    # اگه ادمین در حال تنظیم «خودتخریب فایل»ه (دامنه یا زمان)
    if _is_admin(message.from_user.id) and message.from_user.id in _pending_selfdestruct:
        stage = _pending_selfdestruct[message.from_user.id]

        if text == "/cancel":
            del _pending_selfdestruct[message.from_user.id]
            await message.answer("لغو شد.")
            return

        if stage == "await_domain":
            domain = text.strip().lower()
            domain = re.sub(r'^https?://', '', domain).split('/')[0]
            if domain.startswith("www."):
                domain = domain[4:]
            if not re.match(r'^[a-z0-9][a-z0-9\-\.]*\.[a-z]{2,}$', domain):
                await message.answer("این یه دامنه‌ی معتبر به نظر نمی‌رسه. دوباره امتحان کن یا /cancel.")
                return

            from db.crud import BotConfigCRUD
            async with async_session_maker() as session:
                domains = await _get_self_destruct_domains(session)
                if domain not in domains:
                    domains.append(domain)
                    await BotConfigCRUD.set(session, "self_destruct_domains", json.dumps(domains))
            del _pending_selfdestruct[message.from_user.id]
            await message.answer(f"✅ دامنه‌ی <code>{html.escape(domain)}</code> اضافه شد به لیست خودتخریب.")
            return

        elif stage == "await_seconds":
            if not text.strip().isdigit():
                await message.answer("فقط عدد بفرست (مثلاً 90). یا /cancel برای لغو.")
                return
            seconds = int(text.strip())
            if not (5 <= seconds <= 3600):
                await message.answer("عدد باید بین ۵ تا ۳۶۰۰ ثانیه (یک ساعت) باشه.")
                return

            from db.crud import BotConfigCRUD
            async with async_session_maker() as session:
                await BotConfigCRUD.set(session, "self_destruct_seconds", str(seconds))
            del _pending_selfdestruct[message.from_user.id]
            await message.answer(f"✅ زمان خودتخریب رو {seconds} ثانیه گذاشتم.")
            return

    # اگه منتظریم کاربر یه اسم انگلیسی دلخواه برای لینک مستقیمِ یه فایلِ
    # مستقیماً آپلودشده (نه لینک) بفرسته
    if message.from_user.id in _pending_upload_direct_link_name:
        pending = _pending_upload_direct_link_name[message.from_user.id]

        if text == "/cancel":
            del _pending_upload_direct_link_name[message.from_user.id]
            await message.answer("لغو شد.")
            return

        from utils.direct_links import sanitize_ascii_filename
        clean_name = sanitize_ascii_filename(text)
        if not clean_name:
            await message.answer(
                "این اسم قابل‌استفاده نیست. فقط حروف/عدد انگلیسی، فاصله، خط‌تیره یا "
                "زیرخط بفرست (بدون فارسی/عربی و بدون کاراکتر خاص)، یا /cancel برای لغو."
            )
            return

        del _pending_upload_direct_link_name[message.from_user.id]
        await _start_upload_direct_link_job(pending, direct_link_filename=clean_name)
        return

    # اگه منتظریم کاربر یه اسم انگلیسی دلخواه برای لینک دانلود مستقیم بفرسته
    if message.from_user.id in _pending_direct_link_name:
        pending = _pending_direct_link_name[message.from_user.id]

        if text == "/cancel":
            del _pending_direct_link_name[message.from_user.id]
            await message.answer("لغو شد.")
            return

        from utils.direct_links import sanitize_ascii_filename
        clean_name = sanitize_ascii_filename(text)
        if not clean_name:
            await message.answer(
                "این اسم قابل‌استفاده نیست. فقط حروف/عدد انگلیسی، فاصله، خط‌تیره یا "
                "زیرخط بفرست (بدون فارسی/عربی و بدون کاراکتر خاص)، یا /cancel برای لغو."
            )
            return

        del _pending_direct_link_name[message.from_user.id]
        await _start_download_job(
            pending["status_chat_id"], pending["status_message_id"],
            pending["job_id"], pending["code"],
            want_direct_link=pending.get("want_direct_link", True), direct_link_filename=clean_name
        )
        return

    # اگه کاربر روی دکمه‌ی «پشتیبانی» زده و منتظریم پیامش رو بفرسته
    if message.from_user.id in _pending_support:
        if text == "/cancel":
            _pending_support.discard(message.from_user.id)
            await message.answer("لغو شد.")
            return

        _pending_support.discard(message.from_user.id)

        if not settings.ADMIN_IDS:
            await message.answer("⚠️ فعلاً پشتیبانی در دسترس نیست، بعداً دوباره امتحان کن.")
            return

        sender_name = message.from_user.full_name or "کاربر"
        sender_username = f"@{message.from_user.username}" if message.from_user.username else "بدون یوزرنیم"
        admin_text = (
            f"📩 <b>پیام پشتیبانی جدید</b>\n\n"
            f"👤 {html.escape(sender_name)} ({sender_username})\n"
            f"🆔 <code>{message.from_user.id}</code>\n\n"
            f"{html.escape(message.text)}\n\n"
            f"<i>برای پاسخ، روی همین پیام ریپلای بزن.</i>"
        )
        delivered = False
        for admin_id in settings.ADMIN_IDS:
            try:
                sent = await bot.send_message(admin_id, admin_text)
                _support_replies[(admin_id, sent.message_id)] = message.from_user.id
                delivered = True
                # جلوگیری از رشد بی‌نهایت حافظه: بعد از ۵۰۰ تا، قدیمی‌ترین‌ها
                # (اول دیکشنری = قدیمی‌ترین) حذف می‌شن - ریپلای روی اون‌قدر
                # قدیمی‌ها از قبل عملاً بی‌معنیه.
                if len(_support_replies) > 500:
                    for old_key in list(_support_replies.keys())[: len(_support_replies) - 500]:
                        _support_replies.pop(old_key, None)
            except Exception as e:
                logger.warning(f"ارسال پیام پشتیبانی به ادمین {admin_id} ناموفق بود: {e}")

        if delivered:
            await message.answer("✅ پیامت برای پشتیبانی ارسال شد، به‌زودی جواب می‌گیری.")
        else:
            await message.answer("⚠️ ارسال پیام پشتیبانی ناموفق بود، بعداً دوباره امتحان کن.")
        return

    # اگه ادمین در حال ویرایش یکی از متن‌هاست، این پیام رو به‌عنوان لینک تلقی نکن
    if message.from_user.id in _pending_text_edit and _is_admin(message.from_user.id):
        key = _pending_text_edit[message.from_user.id]
        from db.crud import BotConfigCRUD

        if text == "/cancel":
            del _pending_text_edit[message.from_user.id]
            await message.answer("لغو شد.")
            return

        if text == "/reset":
            async with async_session_maker() as session:
                await BotConfigCRUD.delete(session, key)
            del _pending_text_edit[message.from_user.id]
            await message.answer("✅ به متن پیش‌فرض برگشت.")
            return

        async with async_session_maker() as session:
            # message.html_text (نه message.text) رو ذخیره می‌کنیم تا هر فرمتی
            # که ادمین تو پیامش گذاشته (بولد/ایتالیک/ایموجی پرمیوم و...) دقیقاً
            # همون‌جوری که فرستاده حفظ بشه، بدون نیاز به تایپ دستی تگ HTML
            await BotConfigCRUD.set(session, key, message.html_text, updated_by=message.from_user.id)
        del _pending_text_edit[message.from_user.id]
        await message.answer("✅ متن جدید ذخیره شد.")
        return

    if not LinkDetector.is_valid_url(text):
        return

    # فقط خودِ لینک رو از متن بیرون بکش، چون کاربر ممکنه قبل/بعد لینک یه توضیح
    # هم نوشته باشه (مثلاً «ایول \n https://...») که نباید جزو URL بشه
    extracted_url = LinkDetector.extract_url(text)
    if not extracted_url:
        return
    text = extracted_url

    # امنیت (SSRF): این URL قراره یا با HEAD/HTTP توسط خودِ بات (probe حجم)
    # یا با yt-dlp/aiohttp توسط worker دانلود بشه. بدون این چک، یه کاربر
    # می‌تونست لینکی مثل http://127.0.0.1:8081/... یا http://169.254.169.254/...
    # (متادیتای ابری) یا یه IP داخلی شبکه بده و بات/سرور رو مجبور کنه بهش
    # ریکوئست بزنه - یعنی بات به‌عنوان پروکسی برای اسکن/دسترسی به شبکه‌ی
    # داخلی سرور استفاده بشه.
    if not await _is_url_safe_to_fetch(text):
        await message.reply("⛔️ این لینک قابل پردازش نیست.")
        return

    detector = LinkDetector()
    site = detector.detect_site(text)
    display_name = LinkDetector.get_display_name(text, site)

    # نوع فایل بر اساس سایت/نوع لینک
    # سایت‌های صوتی → AUDIO
    # گیت‌هاب، فایل مستقیم اپلیکیشن/آرشیو، فایل مستقیم صوتی/ویدیویی → DOCUMENT (دانلود مستقیم)
    # بقیه → VIDEO (از طریق yt-dlp)
    DIRECT_DOWNLOAD_SITES = (
        SiteType.GITHUB,
        SiteType.DIRECT_FILE,
        SiteType.DIRECT_AUDIO,
        SiteType.DIRECT_VIDEO,
        SiteType.APKPURE,
        SiteType.PIXELDRAIN,
        SiteType.GOFILE,
        SiteType.DROPBOX,
        SiteType.FILELU,
        SiteType.MEDIAFIRE,
        SiteType.GOOGLE_DRIVE,
        SiteType.MEGA,
    )
    AUDIO_SITES = (
        SiteType.SPOTIFY, SiteType.YOUTUBE_MUSIC,
        SiteType.SOUNDCLOUD, SiteType.APPLE_MUSIC,
    )

    if site in AUDIO_SITES:
        file_type = FileType.AUDIO
    elif site in DIRECT_DOWNLOAD_SITES:
        file_type = FileType.DOCUMENT
    elif site == SiteType.PINTEREST:
        # پین‌ها کیفیت‌های مختلف (1080p/720p/...) ندارن، پس منوی کیفیت بی‌معنیه.
        # نوع واقعی (عکس یا ویدیو) رو پایین‌تر، بعد از گرفتن اطلاعات واقعیِ پین
        # (fetch_pinterest_pin) مشخص می‌کنیم؛ این فقط یه مقدار اولیه‌ست.
        file_type = FileType.PHOTO
    elif site == SiteType.TWITTER:
        # فقط یه مقدار اولیه؛ چون توییتر بدون yt-dlp و مستقیم از API خودش
        # جواب می‌گیریم (پایین‌تر)، همون‌جا دقیقاً می‌فهمیم عکسه یا ویدیو.
        file_type = FileType.VIDEO
    else:
        file_type = FileType.VIDEO

    from db.session import async_session_maker
    async with async_session_maker() as session:
        processing_text = await get_text(session, "text_processing", display_name=display_name)
    status_msg = await message.reply(processing_text)

    # چک سقف روزانه *قبل* از هر کار سنگین (yt-dlp/اسکرپرهای توییتر/پینترست/
    # اینستاگرام و چک کش) - قبلاً بعد از استخراج metadata بود، یعنی کاربری که
    # سهمیه‌ش پر شده بود منابع CPU/شبکه‌ی سرور رو بابت پردازشی مصرف می‌کرد
    # که هرگز به دانلود نمی‌رسید.
    if not _is_admin(message.from_user.id):
        async with async_session_maker() as session:
            user = await UserCRUD.get_or_create(
                session,
                telegram_id=message.from_user.id,
                username=message.from_user.username,
                full_name=message.from_user.first_name or "کاربر"
            )
            today_count = await DownloadJobCRUD.count_today_by_user(session, user.id, only_uploads=False)
            if today_count >= settings.MAX_DAILY_DOWNLOADS:
                limit_text = await get_text(session, "text_daily_limit", limit=settings.MAX_DAILY_DOWNLOADS)
                await status_msg.edit_text(limit_text)
                return

    try:
        # چک کش برای صوت، فایل مستقیم و توییتر معنی داره چون این‌ها مفهوم
        # "کیفیت" ندارن (توییتر هم چون منوی کیفیت نداره، همیشه "best"ه).
        # برای ویدیوی معمولی، چون ممکنه کاربر این‌بار کیفیت دیگه‌ای بخواد،
        # صبر می‌کنیم تا خودش کیفیت رو انتخاب کنه و بعد کش رو چک می‌کنیم.
        cached = None
        if file_type != FileType.VIDEO or site in (SiteType.TWITTER, SiteType.PINTEREST):
            from db.crud import FileCacheCRUD
            from utils.cache_key import get_cache_hash

            cache_hash = get_cache_hash(text, "best", file_type.value)
            async with async_session_maker() as cache_session:
                cached = await FileCacheCRUD.get_by_hash(cache_session, cache_hash)

        if cached and cached.telegram_file_id:
            await _send_cached_file(message, cached)
            try:
                await status_msg.delete()
            except Exception as e:
                logger.warning(f"حذف پیام وضعیت ناموفق بود: {e}")
            return

        # لینک‌های مستقیم (گیت‌هاب / apk / exe / zip / ...) نیازی به metadata از yt-dlp ندارن
        tweet_data = None
        pinterest_media = None
        instagram_post = None
        if site in DIRECT_DOWNLOAD_SITES:
            metadata = None
        elif site == SiteType.TWITTER:
            # مسیر جدا برای توییتر/ایکس: بدون yt-dlp، مستقیم از API خودِ توییتر
            from utils.twitter_downloader import fetch_tweet
            try:
                tweet_data = await fetch_tweet(text)
            except Exception as e:
                logger.warning(f"گرفتن اطلاعات توییتر ناموفق بود: {e}")
                tweet_data = None

            if not tweet_data or not tweet_data.medias:
                await status_msg.edit_text(
                    "❌ نتونستم مدیایی از این لینک توییتر/ایکس پیدا کنم.\n"
                    "ممکنه توییت فقط متنی باشه، حذف شده باشه، یا اکانتش خصوصی باشه."
                )
                return

            file_type = FileType.VIDEO if any(m.type == "video" for m in tweet_data.medias) else FileType.PHOTO
            metadata = None
        elif site == SiteType.PINTEREST:
            # مسیر جدا برای پینترست: بدون yt-dlp (اکسترکتورش پین عکسی رو اصلاً
            # پشتیبانی نمی‌کنه و برای ویدیو هم گاهی ۴۰۴ تقلبی می‌ده)، مستقیم از
            # API داخلی خودِ pinterest.com می‌گیریم و بر همون اساس واقعاً
            # تشخیص می‌دیم پین عکسه یا ویدیو.
            from utils.pinterest_downloader import fetch_pinterest_pin
            try:
                pinterest_media = await fetch_pinterest_pin(text)
            except Exception as e:
                logger.warning(f"گرفتن اطلاعات پین پینترست ناموفق بود: {e}")
                pinterest_media = None

            if not pinterest_media:
                await status_msg.edit_text(
                    "❌ نتونستم این پین پینترست رو پیدا کنم.\n"
                    "ممکنه پین حذف شده باشه یا لینک اشتباه باشه."
                )
                return

            file_type = FileType.VIDEO if pinterest_media.type == "video" else FileType.PHOTO
            metadata = None
        else:
            logger.info(f"استخراج metadata از: {site.value}")
            cookie_file = settings.INSTAGRAM_COOKIE_FILE if site in (SiteType.INSTAGRAM, SiteType.INSTAGRAM_REEL) else None
            metadata = await MetadataExtractor.extract_from_url(text, cookie_file=cookie_file)

            # برای اینستاگرام همیشه (چه yt-dlp موفق بشه چه نشه) از API داخلی
            # اینستاگرام هم نوع واقعی پست رو می‌گیریم. علتش اینه که yt-dlp برای
            # بعضی پست‌های عکسی/کاروسل هم یه JSON بدون خطا (returncode==0)
            # برمی‌گردونه که ظاهرش شبیه متادیتای معتبره، در حالی که پست واقعاً
            # عکسه؛ پس شرط قبلی «فقط اگه metadata خالی بود» هیچ‌وقت فعال نمی‌شد و
            # این‌جور پست‌ها اشتباهی VIDEO تشخیص داده می‌شدن (و منوی کیفیت
            # بی‌ربط نشون داده می‌شد). حالا مستقل از نتیجه‌ی yt-dlp همیشه چک
            # می‌کنیم تا نوع واقعی (عکس/کاروسل/ویدیو) درست مشخص بشه؛ متادیتای
            # yt-dlp هم برای عنوان/توضیحات همچنان استفاده می‌شه.
            if site in (SiteType.INSTAGRAM, SiteType.INSTAGRAM_REEL):
                from utils.instagram_downloader import fetch_instagram_post
                try:
                    instagram_post = await fetch_instagram_post(text, cookie_file=cookie_file)
                except Exception as e:
                    logger.warning(f"گرفتن اطلاعات پست اینستاگرام ناموفق بود: {e}")
                    instagram_post = None

                if metadata is not None:
                    # yt-dlp فقط برای پست‌های واقعاً ویدیویی (ریل/ویدیو) موفق
                    # می‌شه؛ برای عکس/کاروسل همیشه fail می‌کنه (طبق مستندات
                    # بالای instagram_downloader.py). پس اگه metadata موفق
                    # شده، این قطعاً یه ویدیوعه - همین رو ملاک اصلی می‌گیریم،
                    # نه instagram_post. چرا نه instagram_post؟ چون API
                    # داخلیِ اینستاگرام برای درخواست‌های anonymous (بدون
                    # کوکی - که الان اول امتحان می‌شه) بعضی‌وقتا برای ریل‌ها
                    # فقط thumbnail (image_versions2) برمی‌گردونه و
                    # video_versions (فیلد محافظت‌شده‌تر) رو حذف می‌کنه؛ یعنی
                    # instagram_post ممکنه یه ریل واقعی رو اشتباهی «عکس»
                    # تشخیص بده - همین باعث می‌شد بات فقط عکسِ preview ریل رو
                    # بفرسته (بدون منوی کیفیت)، نه خودِ ویدیو.
                    file_type = FileType.VIDEO
                elif instagram_post and instagram_post.items:
                    # yt-dlp fail کرده (پس تقریباً حتماً پست عکسی/کاروسله)؛
                    # اینجا از instagram_post برای تشخیص دقیق نوع و گرفتن
                    # آیتم‌های کاروسل استفاده می‌کنیم.
                    file_type = (
                        FileType.VIDEO if instagram_post.items[0].type == "video" else FileType.PHOTO
                    )
                # اگه هم yt-dlp هم instagram_post هیچی ندادن، همون پیش‌فرض
                # اولیه (VIDEO) می‌مونه - امن‌تره چون اکثر لینک‌های اینستاگرامی
                # که کاربرا می‌فرستن ویدیو/ریل هستن.

        async with async_session_maker() as session:
            user = await UserCRUD.get_or_create(
                session,
                telegram_id=message.from_user.id,
                username=message.from_user.username,
                full_name=message.from_user.first_name or "کاربر"
            )

            twitter_title = None
            twitter_description = None
            if site == SiteType.TWITTER and tweet_data:
                twitter_title = f"@{tweet_data.author_username}" if tweet_data.author_username else tweet_data.author_name
                if tweet_data.text:
                    twitter_title += f" — {tweet_data.text[:60]}"
                    # کپشن کامل (بدون بریدن) که موقع ارسال فایل نهایی استفاده می‌شه
                    twitter_description = tweet_data.text

            # پینترست: pinterest_media.title همون کپشن/عنوان واقعیِ پینه (از
            # همون API داخلی pinterest.com) - قبلاً این‌جا اصلاً خونده نمی‌شد و
            # description همیشه None می‌موند برای پین‌ها.
            pinterest_description = pinterest_media.title if pinterest_media else None

            # اینستاگرام: وقتی yt-dlp نتونسته metadata بگیره (پست عکسی/کاروسل) و
            # از API داخلی اینستاگرام استفاده کردیم، caption همون‌جا (instagram_post)
            # می‌مونه ولی قبلاً هیچ‌وقت به description منتقل نمی‌شد.
            instagram_description = instagram_post.caption if instagram_post else None

            job = await DownloadJobCRUD.create(
                session,
                user_id=user.id,
                chat_id=message.chat.id,
                message_id=message.message_id,
                status_message_id=status_msg.message_id,
                url=text,
                detected_site=site.value,
                raw_domain=urlparse(text).netloc.lower(),
                file_type=file_type,
                title=twitter_title or (metadata.title if metadata else None),
                # description کپشن کامل رو نگه می‌داره (title فقط خلاصه‌ست)؛ قبلاً این
                # فیلد اصلاً ذخیره نمی‌شد و کپشن کامل پست هیچ‌وقت به فایل تحویلی نمی‌رسید
                description=(
                    twitter_description
                    or pinterest_description
                    or instagram_description
                    or (metadata.description if metadata else None)
                ),
                artist=metadata.artist if metadata else None,
                album=metadata.album if metadata else None,
                duration=metadata.duration if metadata else None,
                thumbnail_url=metadata.thumbnail_url if metadata else None,
            )

            logger.info(f"کار دانلود ایجاد شد: {job.id}")

        msg_text = "✅ <b>اطلاعات:</b>\n\n"
        if site == SiteType.TWITTER and tweet_data:
            caption = tweet_data.text[:200] + ("…" if len(tweet_data.text) > 200 else "")
            msg_text += f"🐦 <b>{html.escape(tweet_data.author_name)}</b> (@{html.escape(tweet_data.author_username)})\n"
            if caption:
                msg_text += f"\n{html.escape(caption)}\n"
        elif metadata:
            msg_text += format_metadata_message(metadata)
        else:
            msg_text += f"<b>📌 لینک:</b> {text[:80]}{'...' if len(text) > 80 else ''}\n"

        if file_type == FileType.VIDEO and site not in (SiteType.TWITTER, SiteType.PINTEREST):
            # برای ویدیو، کیفیت رو از خود کاربر می‌پرسیم و دانلود رو شروع نمی‌کنیم
            msg_text += "\n<b>🎚 کیفیت مورد نظرت رو انتخاب کن:</b>"
            await status_msg.edit_text(msg_text, reply_markup=_quality_keyboard(job.id))
            return

        # لینک‌های فایل مستقیم (جدا از پلتفرم‌ها - نه یوتیوب/اینستاگرام/...) که
        # احتمالاً یه فیلم/سریاله (گوفایل، دراپ‌باکس، مدیافایر، پیکسل‌درین،
        # فایل‌لو، گوگل‌درایو، مگا، یا لینک مستقیم .mp4/.mkv/...): اگه کاربر تو
        # /settings «نمایش گزینه‌ی تغییر نام» رو روشن کرده، قبل از دانلود
        # می‌پرسیم اسم دلخواه می‌خواد یا نه. (thumbnail دلخواه هم خودکار از
        # همون تنظیمات کاربر اعمال می‌شه - نیازی به پرسیدن جدا نیست.)
        RENAME_ELIGIBLE_SITES = (
            SiteType.DIRECT_VIDEO, SiteType.PIXELDRAIN, SiteType.GOFILE,
            SiteType.DROPBOX, SiteType.FILELU, SiteType.MEDIAFIRE,
            SiteType.GOOGLE_DRIVE, SiteType.MEGA,
        )
        if site in RENAME_ELIGIBLE_SITES:
            async with async_session_maker() as rename_check_session:
                rename_user = await UserCRUD.get_by_pk(rename_check_session, job.user_id)
            if rename_user and rename_user.show_rename_option:
                await _maybe_start_or_ask_rename(
                    status_msg.chat.id, status_msg.message_id, job.id, "best",
                    want_direct_link=False, from_user_id=message.from_user.id
                )
                return

        # لینک‌های فایل مستقیم می‌تونن چند گیگ باشن بدون این‌که از قبل بدونیم -
        # قبل از این‌که پهنای باند سرور رو بابتش خرج کنیم، با یه HEAD سبک حجمش
        # رو می‌پرسیم؛ اگه بزرگ بود، به‌جای دانلود خودکار از کاربر تاییدیه می‌گیریم.
        if site in DIRECT_DOWNLOAD_SITES:
            size_mb = await _probe_remote_size_mb(text)
            if size_mb and size_mb > settings.MAX_WARN_SIZE_MB:
                confirm_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="▶️ باشه، دانلودش کن", callback_data=f"confirm_large_dl:{job.id}", style="primary")],
                    [InlineKeyboardButton(text="❌ لغو", callback_data=f"cancel_dl:{job.id}", style="danger")],
                ])
                await status_msg.edit_text(
                    msg_text + f"\n\n⚠️ <b>این فایل حدود {size_mb:.0f} مگابایته</b> - ممکنه دانلودش طول بکشه. ادامه بدم؟",
                    reply_markup=confirm_keyboard
                )
                return

        await _kickoff_direct_download(status_msg.chat.id, status_msg.message_id, job.id, msg_text)

    except Exception as e:
        logger.error(f"خطا: {e}", exc_info=True)

        retry_keyboard = None
        job = locals().get("job")
        if job:
            retry_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔁 تلاش دوباره", callback_data=f"retry_dl:{job.id}", style="primary")
            ]])

        await status_msg.edit_text(
            f"❌ خطا در پردازش!\n\n"
            # فقط اسم تایپ خطا (بدون متنش) - str(e) گاهی مسیرهای داخلی سرور/
            # توکن رو لو می‌ده؛ متن کامل در لاگ ثبت شده.
            f"کد خطا: <code>{html.escape(type(e).__name__)}</code>",
            reply_markup=retry_keyboard
        )


async def _is_url_safe_to_fetch(url: str) -> bool:
    """جلوگیری از SSRF - منطق واقعی تو utils/url_safety.py (مشترک با ورکر).

    علاوه بر IPهای لوکال/خصوصی، فرم‌های عددی IP (اعشاری مثل 2130706433،
    هگز مثل 0x7f000001، اکتال مثل 0177.0.0.1) و آدرس‌های IPv6 با IPv4
    نگاشت‌شده (::ffff:127.0.0.1) هم رد می‌شن؛ به‌علاوه به‌صورت پیش‌فرض host
    واقعاً resolve می‌شه و اگه هر IP نتیجه خصوصی بود، URL رد می‌شه (جلوی
    DNS rebinding و دامنه‌هایی که به IP داخلی اشاره می‌کنن)."""
    return await url_is_safe_to_fetch(url)


async def _probe_remote_size_mb(url: str) -> float | None:
    """با یه HEAD request سبک، حجم فایل رو (اگه سرور Content-Length بده) از
    قبل می‌پرسه - بدون این‌که واقعاً چیزی دانلود بشه. اگه سرور جواب نداد/
    هدر رو نداد/تایم‌اوت شد، None برمی‌گردونه (یعنی: نمی‌دونیم، پس هشدار نده،
    بلاک‌کردن دانلود به‌خاطر یه چک اختیاری اشتباهه).

    ریدایرکت‌ها دستی دنبال می‌شن (allow_redirects=False) و مقصد هر hop قبل
    از درخواست دوباره SSRF-check می‌شه - بدون این، یه لینک عمومی که 302 به
    169.254.169.254 می‌داد، probe رو مجبور به زدن به متادیتای ابری می‌کرد."""
    import aiohttp
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as http_session:
            current_url = url
            for _hop in range(5):
                if not await _is_url_safe_to_fetch(current_url):
                    return None
                async with http_session.head(current_url, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location")
                        if not location:
                            return None
                        current_url = str(resp.url.join(location))
                        continue
                    size = resp.headers.get("Content-Length")
                    if size and size.isdigit():
                        return int(size) / (1024 * 1024)
                    return None
    except Exception as e:
        logger.debug(f"HEAD probe برای حجم فایل ناموفق بود ({url[:60]}...): {e}")
    return None


async def _kickoff_direct_download(chat_id: int, status_message_id: int, job_id: int, msg_text: str) -> bool:
    """تیکه‌ی مشترکِ «واقعاً بفرست به صف celery» - هم از مسیر عادیِ فایل مستقیم
    صدا زده می‌شه، هم از دکمه‌ی تاییدِ «حجمش بزرگه ولی می‌خوام ادامه بدم».

    False برمی‌گردونه اگه ادعای اتمیک جاب شکست بخوره (یعنی یه کلیک دیگه همزمان
    جاب رو گرفته یا لغو شده) تا caller به‌جای dispatch، به کاربر پیام بده."""
    from db.session import async_session_maker
    from db.crud import DownloadJobCRUD

    # ادعای اتمیک قبل از هر ادیت پیامی - اگه شکست بخوره هیچ چیز تغییر نکنه
    async with async_session_maker() as session:
        claimed = await DownloadJobCRUD.claim_for_processing(
            session, job_id,
            from_statuses=(DownloadStatus.PENDING, DownloadStatus.DETECTING),
            to_status=DownloadStatus.DOWNLOADING,
        )
    if not claimed:
        return False

    try:
        async with async_session_maker() as _qsession:
            active_count = await DownloadJobCRUD.count_active(_qsession)
    except Exception:
        active_count = 0

    final_text = msg_text + "\n<i>🔄 در حال دانلود...</i>"
    if active_count > 1:
        final_text += f"\n🔢 <i>تو صف: {active_count} دانلود در جریانه...</i>"

    cancel_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ لغو", callback_data=f"cancel_dl:{job_id}", style="danger")
    ]])
    try:
        await bot.edit_message_text(final_text, chat_id=chat_id, message_id=status_message_id, reply_markup=cancel_keyboard)
    except Exception as e:
        logger.warning(f"ویرایش پیام وضعیت برای job {job_id} ناموفق بود: {e}")

    result = download_file.delay(job_id, "best")
    async with async_session_maker() as task_session:
        await DownloadJobCRUD.set_celery_task_id(task_session, job_id, result.id)
    return True


@dp.callback_query(F.data.startswith("confirm_large_dl:"))
async def confirm_large_dl_callback(callback: CallbackQuery):
    from db.session import async_session_maker
    from db.crud import DownloadJobCRUD

    job_id = int(callback.data.split(":", 1)[1])

    async with async_session_maker() as session:
        job = await DownloadJobCRUD.get_by_id(session, job_id)
        if not job:
            await callback.answer("این درخواست پیدا نشد.", show_alert=True)
            return
        if not await _assert_job_owner(session, job, callback):
            return

    ok = await _kickoff_direct_download(
        callback.message.chat.id, callback.message.message_id, job_id,
        callback.message.text or callback.message.html_text or ""
    )
    if ok:
        await callback.answer()
    else:
        # ادعای اتمیک شکست خورده (دابل‌کلیک یا لغو همزمان) - dispatch نشده
        await callback.answer("این درخواست قبلاً پردازش شده.", show_alert=True)


async def _assert_job_owner(session, job, callback: CallbackQuery) -> bool:
    """چک می‌کنه این کالبک واقعاً از طرف صاحبِ همون جاب (یا ادمین) اومده.

    چرا لازمه: job_id فقط یه عدد صعودی معمولیه (۱، ۲، ۳...) که تو خودِ
    callback_data (که رو دکمه‌های تلگرام دیده می‌شه) قابل‌مشاهده و
    به‌راحتی قابل‌حدس‌زدنه. تلگرام خودش callback_data رو امضا/چک نمی‌کنه
    که واقعاً همون کاربری که دکمه رو می‌بینه فرستادتش - یعنی هر کاربری که
    اصلاً یه پیام با دکمه از این بات داشته باشه (حتی مال خودش)، از لحاظ
    فنی می‌تونه یه callback با data دلخواه (مثلاً cancel_dl:5 یا
    quality:5:best) بفرسته، صرف‌نظر از این‌که رو کدوم دکمه کلیک کرده. بدون
    این چک، یه کاربر می‌تونست دانلود/لینک‌مستقیم کاربر دیگه‌ای رو لغو،
    retry، رنیم یا حذف کنه، فقط با حدس زدن job_id."""
    if _is_admin(callback.from_user.id):
        return True
    owner = await UserCRUD.get_by_pk(session, job.user_id)
    if not owner or owner.telegram_id != callback.from_user.id:
        await callback.answer("⛔️ این دانلود مال تو نیست.", show_alert=True)
        return False
    return True


@dp.callback_query(F.data.startswith("cancel_dl:"))
async def cancel_download_callback(callback: CallbackQuery):
    from db.session import async_session_maker
    from db.crud import DownloadJobCRUD
    from db.models import DownloadStatus

    job_id = int(callback.data.split(":", 1)[1])

    async with async_session_maker() as session:
        job = await DownloadJobCRUD.get_by_id(session, job_id)
        if not job:
            await callback.answer("این دانلود پیدا نشد.", show_alert=True)
            return
        if not await _assert_job_owner(session, job, callback):
            return

        if job.status in (DownloadStatus.DONE, DownloadStatus.FAILED, DownloadStatus.CANCELLED):
            await callback.answer("این دانلود دیگه قابل لغو نیست.", show_alert=True)
            return

        if job.celery_task_id:
            try:
                celery_app.control.revoke(job.celery_task_id, terminate=True)
            except Exception as e:
                logger.warning(f"لغو تسک celery ناموفق بود: {e}")

        await DownloadJobCRUD.update_status(session, job_id, DownloadStatus.CANCELLED)
        cancelled_text = await get_text(session, "text_cancelled")

    _pending_direct_link_name.pop(callback.from_user.id, None)
    _pending_upload_direct_link_name.pop(callback.from_user.id, None)
    await callback.answer("لغو شد.")
    await callback.message.edit_text(cancelled_text)


@dp.callback_query(F.data.startswith("retry_dl:"))
async def retry_download_callback(callback: CallbackQuery):
    from db.session import async_session_maker
    from db.crud import DownloadJobCRUD
    from db.models import DownloadStatus

    job_id = int(callback.data.split(":", 1)[1])

    async with async_session_maker() as session:
        job = await DownloadJobCRUD.get_by_id(session, job_id)
        if not job:
            await callback.answer("این دانلود پیدا نشد.", show_alert=True)
            return
        if not await _assert_job_owner(session, job, callback):
            return

        # ادعای اتمیک (FAILED/PENDING/DETECTING → DOWNLOADING): دابل‌کلیک روی
        # «تلاش دوباره» قبلاً دوبار dispatch می‌کرد (هر دو بار status=PENDING
        # می‌شد و چکی جلوش نبود). ریست error_msg/finished_at و جایگزینی
        # status_message_id هم در همون UPDATE اتمیک انجام می‌شه - وگرنه بعدش
        # پیام قدیمی (که خودش قبلاً پاک شده) هدف حذف قرار می‌گرفت و پیام
        # «در حال پردازش دوباره» برای همیشه تو چت می‌موند.
        claimed = await DownloadJobCRUD.claim_for_processing(
            session, job_id,
            from_statuses=(
                DownloadStatus.PENDING, DownloadStatus.DETECTING, DownloadStatus.FAILED,
            ),
            to_status=DownloadStatus.DOWNLOADING,
            error_msg=None, finished_at=None,
            status_message_id=callback.message.message_id,
        )
        if not claimed:
            await callback.answer("این دانلود قبلاً شروع شده.", show_alert=True)
            return
        result = download_file.delay(job.id, job.selected_quality or "best")
        await DownloadJobCRUD.set_celery_task_id(session, job_id, result.id)

    await callback.answer("در حال تلاش مجدد...")
    cancel_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ لغو", callback_data=f"cancel_dl:{job_id}", style="danger")
    ]])
    await callback.message.edit_text(
        f"🔄 درحال پردازش دوباره...\n\n⏳ لطفا صبر کنید...",
        reply_markup=cancel_keyboard
    )


@dp.callback_query(F.data.startswith("quality:"))
async def quality_callback(callback: CallbackQuery):
    from db.session import async_session_maker
    from db.crud import DownloadJobCRUD, FileCacheCRUD
    from utils.cache_key import get_cache_hash

    _, job_id_str, code = callback.data.split(":", 2)
    job_id = int(job_id_str)

    async with async_session_maker() as session:
        job = await DownloadJobCRUD.get_by_id(session, job_id)
        if not job:
            await callback.answer("این درخواست پیدا نشد.", show_alert=True)
            return
        if not await _assert_job_owner(session, job, callback):
            return

        if code == "audio":
            new_file_type = FileType.AUDIO
        elif code == "video_note":
            new_file_type = FileType.VIDEO_NOTE
        else:
            new_file_type = FileType.VIDEO

        # ادعای اتمیک جاب (compare-and-set روی status): قبلاً چک «قبلاً پردازش
        # شده» + update_status(PENDING) مسابقه داشت - دو کلیکِ خیلی سریع هر دو
        # status=PENDING می‌دیدن و هر دو dispatch می‌کردن (دوبار دانلود).
        # الان UPDATE فقط وقتی status=PENDING باشه انجام می‌شه و rowcount صفر
        # یعنی یکی دیگه (کلیک/لغو) قبلاً جاب رو گرفته.
        claimed = await DownloadJobCRUD.claim_for_processing(
            session, job_id,
            from_statuses=(DownloadStatus.PENDING,),
            to_status=DownloadStatus.DETECTING,
            file_type=new_file_type, selected_quality=code,
        )
        if not claimed:
            await callback.answer("این درخواست قبلاً پردازش شده.", show_alert=True)
            return

        # چک کش مخصوص همین کیفیت (نه لزوماً "best")
        cache_hash = get_cache_hash(job.url, code, new_file_type.value)
        cached = await FileCacheCRUD.get_by_hash(session, cache_hash)

        if cached and cached.telegram_file_id:
            # اگه لینک مستقیم فعاله، باید بازم از کاربر بپرسیم که آیا لینک مستقیم
            # هم می‌خواد یا نه - چون فایل کش‌شده فقط file_id تلگرامه، روی دیسک
            # سرور نیست، پس اگه بی‌سوال مستقیم بفرستیمش، هیچ‌وقت گزینه‌ی لینک
            # مستقیم به این کاربر نشون داده نمی‌شه (باگی که قبلاً وجود داشت).
            if settings.DIRECT_LINK_ENABLED:
                await callback.answer()
                quality_label = QUALITY_LABELS.get(code, code)
                cached_link_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ بله، لینک مستقیم هم بده", callback_data=f"dlinkc:{job_id}:{code}:1", style="success")],
                    [InlineKeyboardButton(text="فقط تو تلگرام بفرست", callback_data=f"dlinkc:{job_id}:{code}:0", style="primary")],
                    [InlineKeyboardButton(text="❌ لغو", callback_data=f"cancel_dl:{job_id}", style="danger")],
                ])
                await callback.message.edit_text(
                    f"✅ کیفیت: {quality_label}\n\n"
                    f"🔗 این فایل قبلاً دانلود شده و از کش می‌فرستمش. "
                    f"علاوه بر آپلود در تلگرام، یه لینک دانلود مستقیم از سرور هم بهت بدم؟",
                    reply_markup=cached_link_keyboard
                )
                return

            await DownloadJobCRUD.update_status(
                session, job_id, DownloadStatus.DONE,
                file_type=new_file_type, selected_quality=code
            )
            await callback.answer()
            await _send_cached_file(callback.message, cached)
            try:
                await callback.message.delete()
            except Exception as e:
                logger.warning(f"حذف پیام وضعیت ناموفق بود: {e}")
            return

    await callback.answer()

    # اگه قابلیت لینک مستقیم روی سرور فعال باشه، قبل از شروع دانلود از کاربر
    # می‌پرسیم که آیا علاوه بر آپلود در تلگرام لینک دانلود مستقیم هم می‌خواد یا نه.
    # اگه غیرفعال باشه، مثل قبل مستقیم می‌ره سراغ دانلود.
    if settings.DIRECT_LINK_ENABLED:
        quality_label = QUALITY_LABELS.get(code, code)
        direct_link_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ بله، لینک مستقیم هم بده", callback_data=f"dlink:{job_id}:{code}:1", style="success")],
            [InlineKeyboardButton(text="فقط تو تلگرام بفرست", callback_data=f"dlink:{job_id}:{code}:0", style="primary")],
            [InlineKeyboardButton(text="❌ لغو", callback_data=f"cancel_dl:{job_id}", style="danger")],
        ])
        await callback.message.edit_text(
            f"✅ کیفیت: {quality_label}\n\n"
            f"🔗 علاوه بر آپلود در تلگرام، یه لینک دانلود مستقیم از سرور هم بهت بدم؟",
            reply_markup=direct_link_keyboard
        )
        return

    await _start_download_job(callback.message.chat.id, callback.message.message_id, job_id, code, want_direct_link=False)


async def _maybe_start_or_ask_rename(
    chat_id: int, status_message_id: int, job_id: int, code: str,
    want_direct_link: bool, from_user_id: int
):
    """قبل از واقعاً استارت‌کردن دانلود، اگه کاربر تو /settings «نمایش گزینه‌ی
    تغییر نام» رو روشن کرده باشه، یه پرامپت تغییر نام نشون می‌ده؛ وگرنه
    مستقیم می‌ره سراغ _start_download_job (رفتار قبلی، بدون تغییر)."""
    from db.session import async_session_maker
    from db.crud import DownloadJobCRUD

    async with async_session_maker() as session:
        job = await DownloadJobCRUD.get_by_id(session, job_id)
        user = await UserCRUD.get_by_pk(session, job.user_id) if job else None
        show_rename = bool(user and user.show_rename_option)

    if not show_rename:
        await _start_download_job(chat_id, status_message_id, job_id, code, want_direct_link=want_direct_link)
        return

    want_int = 1 if want_direct_link else 0
    rename_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ تغییر نام فایل", callback_data=f"rn_ask:{job_id}:{code}:{want_int}", style="primary")],
        [InlineKeyboardButton(text="▶️ رد کن، اسم پیش‌فرض باشه", callback_data=f"rn_skip:{job_id}:{code}:{want_int}")],
        [InlineKeyboardButton(text="❌ لغو", callback_data=f"cancel_dl:{job_id}", style="danger")],
    ])
    try:
        await bot.edit_message_text(
            "✏️ می‌خوای اسم فایل رو خودت انتخاب کنی؟",
            chat_id=chat_id, message_id=status_message_id,
            reply_markup=rename_keyboard,
        )
    except Exception as e:
        logger.warning(f"نمایش پرامپت تغییر نام برای job {job_id} ناموفق بود: {e}")
        await _start_download_job(chat_id, status_message_id, job_id, code, want_direct_link=want_direct_link)


@dp.callback_query(F.data.startswith("rn_ask:"))
async def rename_ask_callback(callback: CallbackQuery):
    from db.session import async_session_maker
    from db.crud import DownloadJobCRUD

    _, job_id_str, code, want_str = callback.data.split(":", 3)
    job_id = int(job_id_str)
    want_direct_link = want_str == "1"

    async with async_session_maker() as session:
        job = await DownloadJobCRUD.get_by_id(session, job_id)
        if not job:
            await callback.answer("این درخواست پیدا نشد.", show_alert=True)
            return
        if not await _assert_job_owner(session, job, callback):
            return

    await callback.answer()
    if not _set_pending_rename(callback.from_user.id, {
        "job_id": job_id,
        "code": code,
        "status_chat_id": callback.message.chat.id,
        "status_message_id": callback.message.message_id,
        "want_direct_link": want_direct_link,
    }):
        await callback.answer("یه تغییرنام دیگه در جریانه؛ اول اونو کامل کن یا /cancel بزن.", show_alert=True)
        return
    skip_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ رد کن، اسم پیش‌فرض باشه", callback_data=f"rn_skip:{job_id}:{code}:{want_str}", style="primary")],
        [InlineKeyboardButton(text="❌ لغو", callback_data=f"cancel_dl:{job_id}", style="danger")],
    ])
    await callback.message.edit_text(
        "✍️ یه اسم انگلیسی کوتاه برای فایل بفرست (فقط حروف/عدد انگلیسی، فاصله، "
        "خط‌تیره یا زیرخط - بدون فارسی).\n\n"
        "مثلاً: <code>my_video</code>\n\n"
        "یا دکمه‌ی «رد کن» رو بزن تا با یه اسم پیش‌فرض بسازمش.",
        reply_markup=skip_keyboard
    )


@dp.callback_query(F.data.startswith("rn_skip:"))
async def rename_skip_callback(callback: CallbackQuery):
    from db.session import async_session_maker
    from db.crud import DownloadJobCRUD

    _, job_id_str, code, want_str = callback.data.split(":", 3)
    job_id = int(job_id_str)
    want_direct_link = want_str == "1"

    async with async_session_maker() as session:
        job = await DownloadJobCRUD.get_by_id(session, job_id)
        if not job:
            await callback.answer("این درخواست پیدا نشد.", show_alert=True)
            return
        if not await _assert_job_owner(session, job, callback):
            return

    _pending_direct_link_name.pop(callback.from_user.id, None)
    await callback.answer()
    await _start_download_job(
        callback.message.chat.id, callback.message.message_id, job_id, code,
        want_direct_link=want_direct_link, direct_link_filename=None
    )


# کاربرهایی که منتظریم یه اسم انگلیسی دلخواه برای لینک مستقیم بفرستن.
# مقدار: {"job_id": ..., "code": ..., "status_chat_id": ..., "status_message_id": ...}
_pending_direct_link_name: dict[int, dict] = {}

# مثل بالا، ولی مخصوص فایل‌هایی که کاربر خودش مستقیم به بات فرستاده (نه لینک).
# مقدار: {"job_id": ...}
_pending_upload_direct_link_name: dict[int, dict] = {}


def _set_pending_rename(user_id: int, pending: dict) -> bool:
    """ست کردن state انتظارِ «اسم دلخواه فایل» برای یه کاربر.

    اگه کاربر از قبل برای یه job دیگه تو حالت انتظارِ اسم باشه، dict قبلی
    overwrite می‌شد و اون job برای همیشه بدون dispatch یتیم می‌موند. False
    برمی‌گردونه تا caller بهش بگه اول اون درخواست رو کامل کنه یا /cancel بزنه."""
    existing = _pending_direct_link_name.get(user_id)
    if existing and existing.get("job_id") != pending.get("job_id"):
        return False
    _pending_direct_link_name[user_id] = pending
    return True


@dp.callback_query(F.data.startswith("dlink:"))
async def direct_link_choice_callback(callback: CallbackQuery):
    from db.session import async_session_maker
    from db.crud import DownloadJobCRUD

    _, job_id_str, code, want_str = callback.data.split(":", 3)
    job_id = int(job_id_str)
    want_direct_link = want_str == "1"

    async with async_session_maker() as session:
        job = await DownloadJobCRUD.get_by_id(session, job_id)
        if not job:
            await callback.answer("این درخواست پیدا نشد.", show_alert=True)
            return
        if job.status not in (DownloadStatus.PENDING, DownloadStatus.DETECTING):
            await callback.answer("این درخواست قبلاً پردازش شده.", show_alert=True)
            return

    await callback.answer()

    if not want_direct_link:
        await _start_download_job(callback.message.chat.id, callback.message.message_id, job_id, code, want_direct_link=False)
        return

    # اگه لینک مستقیم خواسته، قبل از دانلود ازش یه اسم انگلیسی می‌خوایم تا
    # لینک نهایی تمیز باشه (نه پر از %DB%... به‌خاطر اسم فارسی)
    if not _set_pending_rename(callback.from_user.id, {
        "job_id": job_id,
        "code": code,
        "status_chat_id": callback.message.chat.id,
        "status_message_id": callback.message.message_id,
        "want_direct_link": True,
    }):
        await callback.answer("یه تغییرنام دیگه در جریانه؛ اول اونو کامل کن یا /cancel بزن.", show_alert=True)
        return
    skip_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ رد کن، اسم پیش‌فرض باشه", callback_data=f"dlink_skip:{job_id}:{code}", style="primary")],
        [InlineKeyboardButton(text="❌ لغو", callback_data=f"cancel_dl:{job_id}", style="danger")],
    ])
    await callback.message.edit_text(
        "✍️ یه اسم انگلیسی کوتاه برای فایل بفرست (فقط حروف/عدد انگلیسی، فاصله، "
        "خط‌تیره یا زیرخط - بدون فارسی).\n\n"
        "مثلاً: <code>my_video</code>\n\n"
        "یا دکمه‌ی «رد کن» رو بزن تا با یه اسم پیش‌فرض بسازمش.",
        reply_markup=skip_keyboard
    )


@dp.callback_query(F.data.startswith("dlinkc:"))
async def direct_link_cached_choice_callback(callback: CallbackQuery):
    """مثل direct_link_choice_callback ولی برای حالتی که فایل از کش تلگرام
    جواب می‌ده. اگه کاربر «نه» بزنه، همون فایل کش‌شده مستقیم فرستاده می‌شه
    (بدون دانلود دوباره). اگه «بله» بزنه، چون فایل کش‌شده روی دیسک سرور
    نیست (فقط file_id تلگرامه)، برای ساختن لینک مستقیم باید واقعاً از منبع
    اصلی (اینستاگرام/...) دوباره دانلود بشه - یعنی از همون مسیر دانلود
    معمولی (نه از کش) رد می‌شه."""
    from db.session import async_session_maker
    from db.crud import DownloadJobCRUD, FileCacheCRUD
    from utils.cache_key import get_cache_hash

    _, job_id_str, code, want_str = callback.data.split(":", 3)
    job_id = int(job_id_str)
    want_direct_link = want_str == "1"

    async with async_session_maker() as session:
        job = await DownloadJobCRUD.get_by_id(session, job_id)
        if not job:
            await callback.answer("این درخواست پیدا نشد.", show_alert=True)
            return
        if job.status not in (DownloadStatus.PENDING, DownloadStatus.DETECTING):
            await callback.answer("این درخواست قبلاً پردازش شده.", show_alert=True)
            return

        if not want_direct_link:
            cache_hash = get_cache_hash(job.url, code, job.file_type.value if job.file_type else None)
            cached = await FileCacheCRUD.get_by_hash(session, cache_hash)
            if not cached or not cached.telegram_file_id:
                # کش بین این دو کلیک از بین رفته - برو مسیر دانلود عادی
                await callback.answer()
                await _start_download_job(callback.message.chat.id, callback.message.message_id, job_id, code, want_direct_link=False)
                return

            await DownloadJobCRUD.update_status(session, job_id, DownloadStatus.DONE)
            await callback.answer()
            await _send_cached_file(callback.message, cached)
            try:
                await callback.message.delete()
            except Exception as e:
                logger.warning(f"حذف پیام وضعیت ناموفق بود: {e}")
            return

    await callback.answer()

    # برای گرفتن لینک مستقیم، اسم دلخواه رو می‌پرسیم - دقیقاً مثل مسیر عادی.
    # ادامه‌ش (dlink_skip یا پیام متنیِ اسم) خودش می‌ره سراغ _start_download_job
    # که یه دانلود واقعی (نه از کش) انجام می‌ده تا فایل واقعی روی دیسک باشه.
    if not _set_pending_rename(callback.from_user.id, {
        "job_id": job_id,
        "code": code,
        "status_chat_id": callback.message.chat.id,
        "status_message_id": callback.message.message_id,
        "want_direct_link": True,
    }):
        await callback.answer("یه تغییرنام دیگه در جریانه؛ اول اونو کامل کن یا /cancel بزن.", show_alert=True)
        return
    skip_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ رد کن، اسم پیش‌فرض باشه", callback_data=f"dlink_skip:{job_id}:{code}", style="primary")],
        [InlineKeyboardButton(text="❌ لغو", callback_data=f"cancel_dl:{job_id}", style="danger")],
    ])
    await callback.message.edit_text(
        "✍️ یه اسم انگلیسی کوتاه برای فایل بفرست (فقط حروف/عدد انگلیسی، فاصله، "
        "خط‌تیره یا زیرخط - بدون فارسی).\n\n"
        "مثلاً: <code>my_video</code>\n\n"
        "یا دکمه‌ی «رد کن» رو بزن تا با یه اسم پیش‌فرض بسازمش.",
        reply_markup=skip_keyboard
    )


@dp.callback_query(F.data.startswith("dlink_skip:"))
async def direct_link_skip_name_callback(callback: CallbackQuery):
    _, job_id_str, code = callback.data.split(":", 2)
    job_id = int(job_id_str)
    _pending_direct_link_name.pop(callback.from_user.id, None)
    await callback.answer()
    await _start_download_job(callback.message.chat.id, callback.message.message_id, job_id, code, want_direct_link=True, direct_link_filename=None)


@dp.callback_query(F.data.startswith("updlink_skip:"))
async def upload_direct_link_skip_callback(callback: CallbackQuery):
    """کاربر روی «رد کن، اسم پیش‌فرض باشه» زد - مخصوص فایل‌های مستقیم آپلودشده."""
    _, job_id_str = callback.data.split(":", 1)
    job_id = int(job_id_str)
    pending = _pending_upload_direct_link_name.pop(callback.from_user.id, None)
    await callback.answer()

    if not pending or pending.get("job_id") != job_id:
        await callback.message.edit_text("⚠️ این درخواست منقضی شده. فایل رو دوباره بفرست.")
        return

    await _start_upload_direct_link_job(pending, direct_link_filename=None)


@dp.callback_query(F.data.startswith("dl_renew:"))
async def cb_renew_direct_link(callback: CallbackQuery):
    """تمدید انقضای یه لینک دانلود مستقیم فعال - هم expiry تو دیتابیس آپدیت
    می‌شه هم خودِ پیام (که زمان باقی‌مونده رو نشون می‌ده) ادیت می‌شه، نه فقط
    یه پیام popup که با بسته‌شدنش دیگه معلوم نیست چقدر زمان مونده."""
    from datetime import datetime, timedelta
    from db.session import async_session_maker
    from db.crud import DownloadJobCRUD

    token = callback.data.split(":", 1)[1]
    async with async_session_maker() as session:
        job = await DownloadJobCRUD.get_by_direct_link_token(session, token)
        if not job or not job.direct_link_token:
            await callback.answer("این لینک دیگه معتبر نیست یا منقضی شده.", show_alert=True)
            return
        # چک مالکیت - بدون این، هر کسی که پیام لینک رو ببینه (مثلاً تو گروه)
        # یا callback جعل کنه می‌تونست لینک/فایل کاربر دیگه رو تمدید کنه
        if not await _assert_job_owner(session, job, callback):
            return
        new_expiry = datetime.utcnow() + timedelta(seconds=settings.DIRECT_LINK_EXPIRE_SECONDS)
        job = await DownloadJobCRUD.renew_direct_link(session, job.id, new_expiry)

    hours = settings.DIRECT_LINK_EXPIRE_SECONDS // 3600
    if callback.message and job and job.direct_link_url:
        try:
            await callback.message.edit_text(
                f"🔗 <b>لینک دانلود مستقیم:</b>\n<code>{html.escape(job.direct_link_url)}</code>\n\n"
                f"⏳ این لینک تا <b>{hours} ساعت</b> دیگه معتبره.",
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=callback.message.reply_markup,
            )
        except Exception as e:
            logger.warning(f"ادیت پیام لینک مستقیم بعد از تمدید (token={token}) ناموفق بود: {e}")
    await callback.answer(f"✅ لینک برای {hours} ساعت دیگه تمدید شد.")


@dp.callback_query(F.data.startswith("dl_stats:"))
async def cb_direct_link_stats(callback: CallbackQuery):
    """نمایش تعداد بازدید/دانلود یه لینک مستقیم."""
    from db.session import async_session_maker
    from db.crud import DownloadJobCRUD

    token = callback.data.split(":", 1)[1]
    async with async_session_maker() as session:
        job = await DownloadJobCRUD.get_by_direct_link_token(session, token)
        if not job:
            await callback.answer("اطلاعاتی پیدا نشد (شاید لینک منقضی شده).", show_alert=True)
            return
        if not await _assert_job_owner(session, job, callback):
            return
    await callback.answer(f"📊 تعداد بار دانلود: {job.direct_link_hit_count or 0}", show_alert=True)


@dp.callback_query(F.data.startswith("dl_qr:"))
async def cb_direct_link_qr(callback: CallbackQuery):
    """ساخت و ارسال QR کد برای یه لینک دانلود مستقیم."""
    import io
    import qrcode
    from aiogram.types import BufferedInputFile
    from db.session import async_session_maker
    from db.crud import DownloadJobCRUD

    token = callback.data.split(":", 1)[1]
    async with async_session_maker() as session:
        job = await DownloadJobCRUD.get_by_direct_link_token(session, token)
        if not job or not job.direct_link_url:
            await callback.answer("این لینک دیگه معتبر نیست.", show_alert=True)
            return
        if not await _assert_job_owner(session, job, callback):
            return
    await callback.answer()

    img = qrcode.make(job.direct_link_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    await callback.message.answer_photo(
        BufferedInputFile(buf.read(), filename="qr.png"),
        caption="📷 برای دانلود مستقیم اسکن کن.",
    )


@dp.callback_query(F.data.startswith("dl_delete:"))
async def cb_delete_direct_link(callback: CallbackQuery):
    """حذف دستیِ فوریِ فایلِ لینک مستقیم از روی سرور (زودتر از انقضای خودکار).

    کاربر بعد از اینکه دانلودش تموم شد، زیر همون پیام لینک این دکمه رو
    می‌زنه: فایل واقعی از دیسک (DIRECT_LINK_DIR/token) پاک می‌شه، رکورد تو
    دیتابیس clear می‌شه، و پیام ادیت می‌شه که دیگه لینک/دکمه‌ها فعال نیستن."""
    from db.session import async_session_maker
    from db.crud import DownloadJobCRUD
    from utils.direct_links import delete_direct_link

    token = callback.data.split(":", 1)[1]
    async with async_session_maker() as session:
        job = await DownloadJobCRUD.get_by_direct_link_token(session, token)
        if not job or not job.direct_link_token:
            await callback.answer("این لینک قبلاً حذف شده یا منقضی شده.", show_alert=True)
            return
        # چک مالکیت - حذف فایل از سرور تخریبیه؛ هر کسی که پیام رو می‌بینه
        # (گروه) یا callback جعل می‌کنه نباید بتونه فایل کاربر دیگه رو حذف کنه
        if not await _assert_job_owner(session, job, callback):
            return
        await DownloadJobCRUD.clear_direct_link(session, job.id)

    delete_direct_link(token)

    if callback.message:
        try:
            await callback.message.edit_text(
                "🗑 فایل از روی سرور حذف شد. لینک دیگه کار نمی‌کنه.",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"ادیت پیام بعد از حذف دستی لینک مستقیم (token={token}) ناموفق بود: {e}")
    await callback.answer("✅ فایل حذف شد.")


async def _prompt_upload_direct_link(message: types.Message, media, file_type: FileType):
    """
    وقتی کاربر مستقیم یه فایل/ویدیو/... (نه لینک) به بات می‌فرسته و
    DIRECT_LINK_ENABLED فعاله: یه job می‌سازه و مثل مسیر لینک‌ها، اول یه اسم
    انگلیسی دلخواه برای لینک مستقیم می‌پرسه (یا رد کردن/پیش‌فرض).
    """
    from db.session import async_session_maker
    from db.crud import DownloadJobCRUD, BotConfigCRUD

    # اگه کاربر برای یه فایل قبلی هنوز تو حالت انتظارِ «اسم دلخواه» باشه،
    # ورود جدید overwrite می‌شد و job قبلی بدون dispatch یتیم می‌موند.
    if message.from_user.id in _pending_upload_direct_link_name:
        await message.reply(
            "یکی یکی 😅 اول اسم فایل قبلی رو بفرست، یا با /cancel لغوش کن، "
            "بعد فایل بعدی رو بفرست."
        )
        return

    async with async_session_maker() as session:
        bot_enabled = await BotConfigCRUD.get_bool(session, "bot_enabled", True)
        disk_state = await BotConfigCRUD.get(session, "disk_space_state") or "ok"
    if not bot_enabled and not _is_admin(message.from_user.id):
        return
    if disk_state == "critical" and not _is_admin(message.from_user.id):
        # نگهبان دیسک - آپلود مستقیم هم یه کپی کامل فایل روی دیسک می‌سازه
        await message.reply(
            "💾 فضای ذخیره‌سازی سرور تقریباً پر شده و ساخت لینک جدید موقتاً بسته‌ست.\n"
            "لطفاً چند ساعت دیگه دوباره امتحان کن."
        )
        return

    file_id = media.file_id
    file_unique_id = media.file_unique_id
    file_size = getattr(media, "file_size", None)

    default_ext = {
        FileType.VIDEO: ".mp4",
        FileType.AUDIO: ".mp3",
        FileType.VIDEO_NOTE: ".mp4",
        FileType.PHOTO: ".jpg",
        FileType.DOCUMENT: "",
    }.get(file_type, "")
    file_name = getattr(media, "file_name", None) or f"upload_{file_unique_id}{default_ext}"

    async with async_session_maker() as session:
        user = await UserCRUD.get_or_create(
            session,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.first_name or "کاربر"
        )

        if not _is_admin(message.from_user.id):
            today_count = await DownloadJobCRUD.count_today_by_user(session, user.id, only_uploads=True)
            if today_count >= settings.MAX_DAILY_DIRECT_UPLOADS:
                limit_text = await get_text(
                    session, "text_daily_limit", limit=settings.MAX_DAILY_DIRECT_UPLOADS
                )
                await message.reply(limit_text)
                return

        max_size = await BotConfigCRUD.get_int(session, "max_file_size_mb", settings.MAX_FILE_SIZE_MB)
        if file_size and (file_size / (1024 * 1024)) > max_size:
            await message.reply(
                f"📦 حجم این فایل بیشتر از سقف مجاز (<b>{max_size} مگابایت</b>) هست."
            )
            return

        job = await DownloadJobCRUD.create(
            session,
            user_id=user.id,
            chat_id=message.chat.id,
            message_id=message.message_id,
            url=f"telegram-upload://{file_unique_id}",
            detected_site="telegram_upload",
            file_type=file_type,
            title=file_name,
            file_size_mb=(file_size / (1024 * 1024)) if file_size else None,
        )

    status_msg = await message.reply(
        "✍️ یه اسم انگلیسی کوتاه برای فایل بفرست (فقط حروف/عدد انگلیسی، فاصله، "
        "خط‌تیره یا زیرخط - بدون فارسی).\n\n"
        "مثلاً: <code>my_file</code>\n\n"
        "یا دکمه‌ی «رد کن» رو بزن تا با یه اسم پیش‌فرض بسازمش.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ رد کن، اسم پیش‌فرض باشه", callback_data=f"updlink_skip:{job.id}", style="primary")],
            [InlineKeyboardButton(text="❌ لغو", callback_data=f"cancel_dl:{job.id}", style="danger")],
        ])
    )

    async with async_session_maker() as session:
        await DownloadJobCRUD.update_status(
            session, job.id, DownloadStatus.PENDING,
            status_message_id=status_msg.message_id,
        )

    _pending_upload_direct_link_name[message.from_user.id] = {
        "job_id": job.id,
        "file_id": file_id,
        "status_chat_id": message.chat.id,
        "status_message_id": status_msg.message_id,
    }


async def _start_upload_direct_link_job(pending: dict, direct_link_filename: str | None):
    """بعد از قطعی‌شدن اسم دلخواه (یا رد کردنش)، واقعاً دانلود فایل از تلگرام
    و ساخت لینک مستقیم رو به صف celery می‌فرسته."""
    from db.session import async_session_maker
    from db.crud import DownloadJobCRUD

    job_id = pending["job_id"]
    file_id = pending["file_id"]
    chat_id = pending["status_chat_id"]
    status_message_id = pending["status_message_id"]

    cancel_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ لغو", callback_data=f"cancel_dl:{job_id}", style="danger")
    ]])
    try:
        await bot.edit_message_text(
            "<i>🔄 در حال دریافت فایل از تلگرام...</i>",
            chat_id=chat_id, message_id=status_message_id,
            reply_markup=cancel_keyboard
        )
    except Exception as e:
        logger.warning(f"ویرایش پیام وضعیت برای job {job_id} ناموفق بود: {e}")

    async with async_session_maker() as session:
        # ادعای اتمیک - همون استدلال _start_download_job (دابل‌کلیک/لغو همزمان)
        claimed = await DownloadJobCRUD.claim_for_processing(
            session, job_id,
            from_statuses=(DownloadStatus.PENDING, DownloadStatus.DETECTING),
            to_status=DownloadStatus.DOWNLOADING,
            want_direct_link=True,
            direct_link_filename=direct_link_filename,
        )
    if not claimed:
        return

    result = download_uploaded_file.delay(job_id, file_id)
    async with async_session_maker() as session:
        await DownloadJobCRUD.set_celery_task_id(session, job_id, result.id)


async def _start_download_job(
    chat_id: int, status_message_id: int, job_id: int, code: str,
    want_direct_link: bool, direct_link_filename: str | None = None
):
    """بعد از قطعی‌شدن کیفیت (و در صورت فعال بودن، انتخاب لینک مستقیم + اسم دلخواه)،
    وضعیت رو نهایی می‌کنه و دانلود رو واقعاً به صف celery می‌فرسته.
    مشترکِ بین مسیر «لینک مستقیم غیرفعاله»، «رد کردن اسم» و «اسم دلخواه فرستاده شد» -
    این آخری از یه پیام متنیِ معمولی میاد (نه callback)، برای همین chat_id/message_id
    رو جدا می‌گیریم تا هم از callback.message هم از پیام متنی صدا زده بشه."""
    from db.session import async_session_maker
    from db.crud import DownloadJobCRUD

    # ادعای اتمیک اول - اگه شکست بخوره (دابل‌کلیک/لغو همزمان)، پیام وضعیت هم
    # نباید ادیت بشه چون dispatch‌ای در کار نیست.
    async with async_session_maker() as session:
        claimed = await DownloadJobCRUD.claim_for_processing(
            session, job_id,
            from_statuses=(DownloadStatus.PENDING, DownloadStatus.DETECTING),
            to_status=DownloadStatus.DOWNLOADING,
            want_direct_link=want_direct_link,
            direct_link_filename=direct_link_filename,
        )
    if not claimed:
        return False

    quality_label = QUALITY_LABELS.get(code, code)
    cancel_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ لغو", callback_data=f"cancel_dl:{job_id}", style="danger")
    ]])

    # اگه هم‌زمان چندتا دانلود دیگه (از هر کاربری) تو صف/در حال پردازشن،
    # به‌جای این‌که کاربر فکر کنه بات گیر کرده، جایگاهش تو صف رو نشونش می‌دیم.
    # (سقف واقعی هم‌زمانی رو worker/concurrency.py با MAX_CONCURRENT_DOWNLOADS
    # اجرا می‌کنه؛ این فقط یه نمایش شفاف از همون وضعیته.)
    queue_line = ""
    try:
        async with async_session_maker() as _qsession:
            active_count = await DownloadJobCRUD.count_active(_qsession)
        if active_count > 1:
            queue_line = f"\n🔢 <i>تو صف: {active_count} دانلود در جریانه...</i>"
    except Exception as e:
        logger.warning(f"شمارش صف برای job {job_id} ناموفق بود: {e}")

    try:
        await bot.edit_message_text(
            f"✅ کیفیت: {quality_label}\n\n<i>🔄 در حال دانلود...</i>{queue_line}",
            chat_id=chat_id, message_id=status_message_id,
            reply_markup=cancel_keyboard
        )
    except Exception as e:
        logger.warning(f"ویرایش پیام وضعیت برای job {job_id} ناموفق بود: {e}")

    result = download_file.delay(job_id, code)
    async with async_session_maker() as session:
        # فقط celery_task_id ست می‌شه (بدون دست‌زدن به status) تا وضعیتی که
        # ورکر ممکنه همزمان ست کرده برگردونده نشه.
        await DownloadJobCRUD.set_celery_task_id(session, job_id, result.id)
    return True


@dp.message()
async def handle_other(message: types.Message):
    # تو گروه‌ها آدم‌ها مدام عکس/ویدیو/فایل بی‌ربط به بات رد و بدل می‌کنن؛
    # اگه به همه‌شون جواب بدیم مزاحم و اسپم می‌شه. این پیام فقط تو چت خصوصی
    # (جایی که واقعاً داریم با خودِ بات صحبت می‌کنیم) معنی داره.
    if message.chat.type != "private":
        return

    # اگه کاربر مستقیم یه فایل/ویدیو/صدا/... به بات بفرسته (نه لینک) و
    # قابلیت لینک مستقیم فعال باشه، همون فایل رو از تلگرام می‌گیریم و
    # به‌جاش یه لینک دانلود مستقیمِ چندساعته (DIRECT_LINK_EXPIRE_SECONDS)
    # بهش می‌دیم - قبلاً این حالت فقط رد می‌شد.
    media_map = [
        (message.video, FileType.VIDEO),
        (message.document, FileType.DOCUMENT),
        (message.audio, FileType.AUDIO),
        (message.voice, FileType.AUDIO),
        (message.video_note, FileType.VIDEO_NOTE),
        (message.animation, FileType.VIDEO),
        (message.photo[-1] if message.photo else None, FileType.PHOTO),
    ]
    media, file_type = next(((m, ft) for m, ft in media_map if m), (None, None))

    if media:
        if not settings.DIRECT_LINK_ENABLED:
            await message.reply(
                "ℹ️ لطفا <b>لینک</b> یا <b>URL</b> ارسال کنید، نه فایل!"
            )
            return
        await _prompt_upload_direct_link(message, media, file_type)


async def set_bot_commands():
    commands = [
        BotCommand(command="start", description="شروع"),
        BotCommand(command="help", description="راهنما"),
        BotCommand(command="settings", description="⚙️ تنظیمات شخصی"),
        BotCommand(command="set_thumb", description="🖼 تنظیم thumbnail دلخواه"),
    ]
    await bot.set_my_commands(commands)

    # دستورات مدیریتی فقط توی منوی خودِ ادمین‌ها دیده بشه، نه بقیه‌ی کاربرها
    if settings.ADMIN_IDS:
        from aiogram.types import BotCommandScopeChat
        admin_commands = commands + [
            BotCommand(command="stats", description="📊 آمار بات"),
            BotCommand(command="topsites", description="🌐 پرکاربردترین سایت‌ها + دامنه‌های ناشناس"),
            BotCommand(command="userinfo", description="👤 اطلاعات و سایت‌های یه کاربر خاص"),
            BotCommand(command="broadcast", description="📢 پیام همگانی به کاربرها"),
            BotCommand(command="cancel", description="✖️ لغو حالت انتظار در جریان"),
            BotCommand(command="toggle", description="🔌 روشن/خاموش کردن بات"),
            BotCommand(command="maxsize", description="⚙️ تنظیم سقف حجم فایل"),
            BotCommand(command="texts", description="✏️ ویرایش متن‌های بات"),
            BotCommand(command="emoji", description="🎨 مدیریت ایموجی‌های پرمیوم"),
            BotCommand(command="forcejoin", description="📢 مدیریت عضویت اجباری"),
            BotCommand(command="selfdestruct", description="💣 مدیریت خودتخریب فایل"),
        ]
        for admin_id in settings.ADMIN_IDS:
            try:
                await bot.set_my_commands(
                    admin_commands,
                    scope=BotCommandScopeChat(chat_id=admin_id)
                )
            except Exception as e:
                logger.warning(f"تنظیم منوی ادمین برای {admin_id} ناموفق بود: {e}")


async def _check_instagram_cookie_freshness():
    """
    قبلاً این تابع فقط سنِ فایل کوکی رو چک می‌کرد - فرض غلطی که با یه کوکی
    ۲روزه‌ی مرده رد شد. حالا واقعاً سشن رو با اینستاگرام تست می‌کنه
    (utils.instagram_downloader.check_cookie_health) تا سر بالا اومدن بات
    بفهمیم سالمه یا نه، نه فقط حدس بزنیم.
    """
    if not settings.INSTAGRAM_COOKIE_FILE:
        return
    from utils.instagram_downloader import check_cookie_health

    healthy, detail = await check_cookie_health(settings.INSTAGRAM_COOKIE_FILE)
    if not healthy:
        warning = f"🍪 کوکی اینستاگرام سالم نیست: {detail}"
        logger.warning(warning)
        for admin_id in settings.ADMIN_IDS:
            try:
                await bot.send_message(admin_id, warning)
            except Exception as e:
                logger.warning(f"ارسال هشدار کوکی اینستاگرام به ادمین {admin_id} ناموفق بود: {e}")


async def main():
    logger.info("🚀 بات شروع می‌شود...")

    global _bot_username
    me = await bot.get_me()
    _bot_username = me.username
    logger.info(f"یوزرنیم بات: @{_bot_username}")

    try:
        await init_db()
        logger.info("✅ دیتابیس آماده شد")
    except Exception as e:
        logger.error(f"خطا در آماده‌سازی دیتابیس: {e}")
        return

    await set_bot_commands()
    await _check_instagram_cookie_freshness()

    logger.info("🔄 polling شروع شد...")
    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            skip_updates=False
        )
    except KeyboardInterrupt:
        logger.info("⏹️ بات متوقف شد")
    finally:
        await close_db()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
