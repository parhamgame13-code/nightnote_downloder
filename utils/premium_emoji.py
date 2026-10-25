"""ایموجی‌های پریمیوم — نگاشت خودکار «کاراکتر معمولی ← custom_emoji_id پریمیوم».

پورت‌شده از همون معماری‌ای که تو sanaei_bot ساختیم. تفاوتش با سیستم قبلیِ
همین پروژه (کاتالوگ + پلیس‌هولدر دستی {e:name} تو /texts، و آیکون دستی
برای هر دکمه‌ی منو): اینجا هیچ پلیس‌هولدری لازم نیست. فقط یه ایموجی
پریمیوم رو از پنل /emoji ثبت کن؛ از اون به بعد همون کاراکتر هرجای متن یا
دکمه‌ی ربات ظاهر بشه (چه هاردکد تو کد، چه از پنل /texts) خودکار پریمیوم
می‌شه — بدون نیاز به دست‌کاری تک‌تک متن‌ها یا دکمه‌ها.

دو نقطه‌ی اتصال واقعی این نگاشت را اعمال می‌کنند (bot/premium_emoji_middleware.py):
  • روی فیلد text/caption هر پیام خروجی → render_message_emoji
  • روی متن هر دکمه‌ی InlineKeyboardButton → render_button_emoji

سوییچ سراسری (is_premium_active) کنترل می‌کند که این تبدیل اصلاً انجام
شود یا نه — پیش‌فرض خاموش. وقتی خاموشه، همه‌جا دقیقاً همون کاراکتر
یونیکدِ اصلی (fallback) نشون داده می‌شه.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from db.crud import BotConfigCRUD
from db.session import async_session_maker

_ACTIVE_KEY = "premium_emoji_active"
_MAP_KEY = "premium_emoji_map"  # JSON: {"emoji_char": "custom_emoji_id", ...}

# برای بازکردن تگ‌های <tg-emoji> که ممکنه قبلاً روی یه متن اعمال شده باشن،
# قبل از این‌که همون متن به‌عنوان برچسب دکمه استفاده بشه (دکمه‌ها اصلاً HTML
# رندر نمی‌کنن) — جلوی «قاطی‌شدن» کاراکتر خام و تگ رو می‌گیره.
_TG_EMOJI_TAG_RE = re.compile(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>')

# کش درون‌حافظه‌ای — چون این ماژول از میدلورِ session صدا زده می‌شه (هر پیام
# خروجی)، نمی‌تونیم هر بار یه رفت‌وبرگشت دیتابیس بزنیم. با هر تغییر
# (register/unregister/toggle) بلافاصله آپدیت می‌شه، پس نیازی به TTL نیست.
_cache: dict = {"loaded": False, "active": False, "map": {}}


async def _ensure_loaded() -> None:
    if _cache["loaded"]:
        return
    async with async_session_maker() as session:
        active_raw = await BotConfigCRUD.get(session, _ACTIVE_KEY)
        map_raw = await BotConfigCRUD.get(session, _MAP_KEY)
    _cache["active"] = active_raw == "1"
    try:
        m = json.loads(map_raw) if map_raw else {}
        _cache["map"] = m if isinstance(m, dict) else {}
    except Exception:
        _cache["map"] = {}
    _cache["loaded"] = True


async def is_premium_active() -> bool:
    """آیا سوییچ سراسری ایموجی پریمیوم روشنه؟ (پیش‌فرض: خاموش)."""
    await _ensure_loaded()
    return _cache["active"]


async def set_premium_active(active: bool) -> None:
    """روشن/خاموش‌کردن سراسری ایموجی پریمیوم — بدون نیاز به ریستارت ربات."""
    async with async_session_maker() as session:
        await BotConfigCRUD.set(session, _ACTIVE_KEY, "1" if active else "0")
    _cache["active"] = active
    _cache["loaded"] = True


async def get_registered_emojis() -> dict:
    """نگاشت فعلی {کاراکتر: custom_emoji_id} — برای نمایش در پنل /emoji."""
    await _ensure_loaded()
    return dict(_cache["map"])


async def register_emoji(char: str, emoji_id: str) -> None:
    """ثبت/به‌روزرسانی یک ایموجی — از پنل /emoji صدا زده می‌شه."""
    await _ensure_loaded()
    _cache["map"][char] = emoji_id
    async with async_session_maker() as session:
        await BotConfigCRUD.set(session, _MAP_KEY, json.dumps(_cache["map"], ensure_ascii=False))


async def unregister_emoji(char: str) -> None:
    await _ensure_loaded()
    _cache["map"].pop(char, None)
    async with async_session_maker() as session:
        await BotConfigCRUD.set(session, _MAP_KEY, json.dumps(_cache["map"], ensure_ascii=False))


async def render_message_emoji(text: str) -> str:
    """جایگزینی خودکار هر ایموجیِ ثبت‌شده‌ی داخل متن با تگ <tg-emoji> پریمیومش.

    اگه سوییچ خاموش باشه یا چیزی ثبت نشده باشه، متن بدون تغییر برمی‌گرده.
    """
    if not text:
        return text
    await _ensure_loaded()
    if not _cache["active"] or not _cache["map"]:
        return text
    for ch, emoji_id in _cache["map"].items():
        if ch and emoji_id and ch in text:
            text = text.replace(ch, f'<tg-emoji emoji-id="{emoji_id}">{ch}</tg-emoji>')
    return text


async def render_button_emoji(text: str) -> tuple[str, Optional[str]]:
    """برای متن یه دکمه: اگه یکی از ایموجی‌های ثبت‌شده داخلش باشه و سوییچ روشن
    باشه، اون کاراکتر از متن حذف می‌شه و آی‌دی‌اش برگردونده می‌شه (تا با
    icon_custom_emoji_id روی خودِ دکمه ست بشه). وگرنه متن بدون تغییر و None.
    """
    if not text:
        return text, None
    text = _TG_EMOJI_TAG_RE.sub(r"\1", text)
    await _ensure_loaded()
    if not _cache["active"] or not _cache["map"]:
        return text, None
    for ch, emoji_id in _cache["map"].items():
        if ch and emoji_id and ch in text:
            cleaned = text.replace(ch, "", 1)
            cleaned = re.sub(r"^\s+", "", cleaned)
            return cleaned, emoji_id
    return text, None
