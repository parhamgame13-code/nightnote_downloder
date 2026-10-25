"""میدلورِ session (نه dispatcher) — روی هر درخواست خروجی به Bot API اجرا
می‌شه، نه روی آپدیت‌های ورودی. با این کار جایگزینی ایموجی پریمیوم روی
*همه‌ی* پیام‌ها و دکمه‌های ربات اعمال می‌شه — چه از get_text() بیان، چه
هاردکد تو کد نوشته شده باشن — بدون نیاز به دست‌کاری تک‌تک نقاط کد.

فقط متدهایی که واقعاً متن/کپشن یا کیبورد اینلاین دارن رو دست‌کاری می‌کنه،
و فقط وقتی مطمئنیم پیام با HTML رندر می‌شه (پیش‌فرض کل ربات هم HTML‌ـه).
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from aiogram.client.default import Default
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.methods import (
    EditMessageCaption,
    EditMessageReplyMarkup,
    EditMessageText,
    SendAnimation,
    SendAudio,
    SendDocument,
    SendMessage,
    SendPhoto,
    SendVideo,
)
from aiogram.types import InlineKeyboardMarkup

from utils.premium_emoji import render_button_emoji, render_message_emoji

logger = logging.getLogger("premium_emoji")

_TEXT_METHODS = (SendMessage, EditMessageText)
_CAPTION_METHODS = (SendPhoto, SendVideo, SendDocument, SendAnimation, SendAudio, EditMessageCaption)
_MARKUP_METHODS = _TEXT_METHODS + _CAPTION_METHODS + (EditMessageReplyMarkup,)


def _safe_parse_mode(pm: Any) -> bool:
    # None یا سنتینلِ حل‌نشده‌ی Default → یعنی پیش‌فرض بات (HTML) اعمال
    # می‌شه؛ رشته‌ی صریح "HTML" هم قابل‌قبوله.
    return pm is None or isinstance(pm, Default) or pm == "HTML"


class PremiumEmojiRequestMiddleware(BaseRequestMiddleware):
    """روی هر SendMessage/EditMessageText/... متن و روی هر دکمه‌ی اینلاین
    داخل reply_markup، از render_message_emoji/render_button_emoji رد می‌شه."""

    async def __call__(
        self,
        make_request: Callable[..., Any],
        bot: Any,
        method: Any,
    ) -> Any:
        if isinstance(method, _TEXT_METHODS):
            pm = getattr(method, "parse_mode", None)
            text = getattr(method, "text", None)
            if _safe_parse_mode(pm) and text:
                method.text = await render_message_emoji(text)
        elif isinstance(method, _CAPTION_METHODS):
            pm = getattr(method, "parse_mode", None)
            caption = getattr(method, "caption", None)
            if _safe_parse_mode(pm) and caption:
                method.caption = await render_message_emoji(caption)

        if isinstance(method, _MARKUP_METHODS):
            markup = getattr(method, "reply_markup", None)
            if isinstance(markup, InlineKeyboardMarkup):
                for row in markup.inline_keyboard:
                    for i, btn in enumerate(row):
                        if not btn.text:
                            continue
                        clean_text, emoji_id = await render_button_emoji(btn.text)
                        if emoji_id:
                            # دکمه‌ها ممکنه frozen باشن؛ به‌جای mutate کردن،
                            # یه نسخه‌ی جدید با model_copy می‌سازیم که همیشه
                            # امنه، صرف‌نظر از تنظیمات pydantic مدل.
                            row[i] = btn.model_copy(
                                update={"text": clean_text, "icon_custom_emoji_id": emoji_id}
                            )

        return await make_request(bot, method)
