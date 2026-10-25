"""
متن‌های قابل‌ویرایش بات - مرکزی، هم برای bot/main.py و هم worker/tasks.py
(چون این دوتا پردازش پایتونِ جدا و مستقلن، این منطق نمی‌تونه فقط تو
main.py بمونه - وگرنه پیام‌هایی که worker مستقیم می‌فرسته (شکست/موفقیت)
هیچ‌وقت از تنظیمات ادمین و کاتالوگ ایموجی پرمیوم استفاده نمی‌کردن.
"""
import re
import json

EDITABLE_TEXTS = {
    "text_start": {
        "label": "🏁 پیام خوش‌آمد (/start)",
        "default": (
            "سلام <b>{name}</b> 👋\n\n"
            "<b>🤖 ربات دانلودر</b> به خدمتتون!\n\n"
            "🔗 هر لینکی ارسال کنید، از هر سایتی!\n\n"
            "⏱ صبر کنید تا دانلود شود و فایل ارسال بشه."
        ),
        "hint": "از {name} برای اسم کاربر استفاده کن.",
    },
    "text_help": {
        "label": "📖 راهنما (/help)",
        "default": (
            "<b>📖 راهنما</b>\n\n"
            "فقط کافیه لینک رو برام بفرستی، خودم تشخیص می‌دم چیه و دانلودش می‌کنم.\n\n"
            "<b>سایت‌های پشتیبانی‌شده:</b>\n"
            "🎬 یوتیوب، اینستاگرام، آپارات، ویمیو، توییتر و بیشتر\n"
            "🎵 اسپاتیفای، ساندکلاود، یوتیوب‌میوزیک، اپل‌میوزیک\n"
            "🐙 لینک‌های مستقیم گیت‌هاب (ریلیز/آرشیو ریپو)\n"
            "📦 لینک‌های مستقیم فایل (.apk, .exe, .msi, .dmg, .zip, .rar و ...)\n"
            "🔗 هر لینک مستقیمی که منجر به دانلود فایل بشه"
        ),
        "hint": None,
    },
    "text_support": {
        "label": "🆘 پیام پشتیبانی",
        "default": (
            "<b>🆘 پشتیبانی</b>\n\n"
            "همینجا پیامتو بنویس، در اسرع وقت جواب داده می‌شه."
        ),
        "hint": None,
    },
    "text_daily_limit": {
        "label": "⛔️ پیام سقف دانلود روزانه",
        "default": (
            "⛔️ سقف دانلود روزانه‌ت پر شده ({limit} تا).\n"
            "فردا دوباره امتحان کن."
        ),
        "hint": "از {limit} برای سقف روزانه استفاده کن.",
    },
    "text_group_welcome": {
        "label": "👋 پیام معرفی هنگام ورود به گروه",
        "default": (
            "سلام به همه! 👋 من <b>ربات دانلودر</b> هستم.\n\n"
            "🔗 کافیه لینک هر ویدیو/آهنگی رو از یوتیوب، اینستاگرام، تیک‌تاک، اسپاتیفای، "
            "ساندکلاود، آپارات و خیلی سایت‌های دیگه تو گروه بفرستید، من خودم دانلودش می‌کنم "
            "و براتون می‌فرستم.\n\n"
            "⚠️ برای اینکه درست کار کنم (پاک کردن پیام‌های «در حال دانلود» و گذاشتن ری‌اکشن) "
            "لطفاً من رو <b>ادمین گروه</b> کنید و حداقل دسترسی «<b>حذف پیام‌ها</b>» رو بهم بدید."
        ),
        "hint": None,
    },
    "text_processing": {
        "label": "🔄 پیام «در حال پردازش» (بعد از فرستادن لینک)",
        "default": (
            "🔄 درحال پردازش...\n\n"
            "{display_name} دریافت شد\n"
            "⏳ لطفا صبر کنید..."
        ),
        "hint": "از {display_name} برای اسم سایت/نوع لینک استفاده کن.",
    },
    "text_force_join_prompt": {
        "label": "📢 پیام عضویت اجباری",
        "default": (
            "⚠️ <b>برای استفاده از بات، اول باید عضو کانال(های) زیر بشی:</b>\n\n"
            "بعد از عضویت، روی «✅ عضو شدم» بزن."
        ),
        "hint": None,
    },
    "text_joined_confirm": {
        "label": "✅ پیام تایید عضویت",
        "default": "✅ عضویتت تایید شد! حالا یه لینک بفرست یا /start بزن.",
        "hint": None,
    },
    "text_cancelled": {
        "label": "🚫 پیام لغو دانلود توسط کاربر",
        "default": "🚫 دانلود توسط کاربر لغو شد.",
        "hint": None,
    },
    "text_failed_wrapper": {
        "label": "❌ سربرگِ پیام شکست دانلود",
        "default": "❌ <b>دانلود انجام نشد</b>\n\n{reason}",
        "hint": "از {reason} برای دلیل دقیق شکست (که خودِ بات می‌سازه) استفاده کن؛ اگه حذفش کنی، فقط سربرگ نشون داده می‌شه.",
    },
}

_EMOJI_PLACEHOLDER_RE = re.compile(r'\{e:([a-zA-Z0-9_]+)\}')


async def _get_emoji_catalog(session) -> dict:
    from db.crud import BotConfigCRUD
    raw = await BotConfigCRUD.get(session, "custom_emoji_catalog")
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


async def apply_emoji_catalog(session, text: str) -> str:
    """جای placeholderهای {e:name} رو با تگ <tg-emoji> واقعی عوض می‌کنه"""
    if "{e:" not in text:
        return text
    catalog = await _get_emoji_catalog(session)
    if not catalog:
        return text

    def _sub(m):
        emoji_id = catalog.get(m.group(1))
        if not emoji_id:
            return m.group(0)  # اسمی که پیدا نشد رو دست‌نخورده بذار
        return f'<tg-emoji emoji-id="{emoji_id}">⭐️</tg-emoji>'

    return _EMOJI_PLACEHOLDER_RE.sub(_sub, text)


async def get_text(session, key: str, **fmt) -> str:
    """متن رو از تنظیمات دیتابیس بخون، اگه ادمین عوضش نکرده بود از پیش‌فرض کد استفاده کن"""
    from db.crud import BotConfigCRUD
    custom = await BotConfigCRUD.get(session, key)
    template = custom if custom else EDITABLE_TEXTS[key]["default"]
    if fmt:
        try:
            template = template.format(**fmt)
        except Exception:
            # اگه ادمین placeholder رو اشتباه نوشته باشه، حداقل بات کرش نکنه
            pass
    return await apply_emoji_catalog(session, template)
