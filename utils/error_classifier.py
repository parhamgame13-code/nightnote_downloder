"""
طبقه‌بندی خطاهای دانلود به پیام‌های دقیق و مشخص فارسی.

این تابع قبلاً مستقیم تو worker/tasks.py بود. جداش کردیم به یه ماژول
مستقل و بدون وابستگی سنگین (بدون Celery/DB/Settings) به دو دلیل:

۱. قابلیت تست: worker/tasks.py برای import شدن نیاز به BOT_TOKEN و اتصال
   واقعی به Redis/DB داره (چون Celery app و DB engine رو در سطح ماژول
   می‌سازه). یه تابع خالص مثل این که فقط ورودی می‌گیره و رشته برمی‌گردونه
   نباید وابسته به این زیرساخت باشه - وگرنه نوشتن یه تست ساده براش هم
   نیاز به راه‌انداختن Redis/DB واقعی داره.
۲. تفکیک مسئولیت: منطق "این خطا یعنی چی" یه قانون کسب‌وکاره، نه بخشی از
   orchestration دانلود. جاش تو utils/ (کنار سایر منطق خالص پروژه مثل
   link_detector.py) منطقی‌تره تا تو worker/tasks.py.
"""
import re
from typing import Optional, Union

import httpx


def classify_download_error(err: Union[Exception, str]) -> Optional[str]:
    """
    یه اکسپشن (مثلاً از httpx تو _download_generic_file) یا متن خام stderr
    یت-دی‌ال‌پی رو می‌گیره و در صورت امکان، به‌جای پیام کلیِ «لینک منقضی شده»،
    یه دلیل دقیق و مشخص برمی‌گردونه. اگه هیچ الگویی تشخیص داده نشه، None
    برمی‌گردونه تا caller از پیام پیش‌فرض خودش استفاده کنه.
    """
    # --- خطاهای HTTP از httpx (دانلود مستقیم فایل) ---
    if isinstance(err, httpx.HTTPStatusError):
        status = err.response.status_code
        if status == 403:
            return (
                "🚫 سرور مقصد درخواست رو رد کرد (403 Forbidden). این لینک احتمالاً "
                "برای IP/شبکه‌ی دستگاهی که اول اونو گرفته سایمن شده و از سرور ربات "
                "قابل دانلود نیست، یا سایت اجازه‌ی دانلود مستقیم نمی‌ده."
            )
        if status == 404:
            return "❌ فایل پیدا نشد (404). احتمالاً حذف شده یا لینک اشتباهه."
        if status == 410:
            return "⏱ این لینک منقضی شده (410 Gone)."
        if status in (401,):
            return "🔒 این لینک نیاز به لاگین/دسترسی داره (401 Unauthorized)."
        if status in (451,):
            return "🚫 این محتوا به‌دلایل قانونی/منطقه‌ای مسدود شده (451)."
        if status == 429:
            return "⏳ سایت مقصد موقتاً محدودیت تعداد درخواست گذاشته (429). چند دقیقه دیگه امتحان کن."
        if 500 <= status < 600:
            return f"🛠 سرور مقصد خودش خطای داخلی داد (کد {status})، ربطی به ربات نداره."
        return f"⚠️ سرور مقصد کد خطای {status} برگردوند."

    if isinstance(err, httpx.ConnectTimeout):
        return "⏱ اتصال به سرور مقصد timeout شد (سرور خیلی کند بود یا در دسترس نبود)."
    if isinstance(err, httpx.ReadTimeout):
        return "⏱ دانلود فایل خیلی طول کشید و timeout شد (سرعت سرور مقصد پایین بود)."
    if isinstance(err, httpx.TimeoutException):
        return "⏱ درخواست به سرور مقصد timeout شد."
    if isinstance(err, httpx.ConnectError):
        msg = str(err).lower()
        if "ssl" in msg or "certificate" in msg:
            return "🔐 خطای SSL هنگام اتصال به سرور مقصد (گواهی نامعتبر یا مسدودسازی TLS)."
        return "🔌 اتصال به سرور مقصد برقرار نشد (دامنه در دسترس نیست یا مسدود شده)."
    if isinstance(err, httpx.TooManyRedirects):
        return "🔁 لینک بین چند صفحه ریدایرکت شد و به حلقه رسید."

    # --- متن خام (اکسپشن به str تبدیل شده یا stderr یت-دی‌ال‌پی) ---
    text = err if isinstance(err, str) else str(err)
    low = text.lower()

    # پاورقی عمومی یت-دی‌ال‌پی («please report this issue...»، «Confirm you are on
    # the latest version...») هیچ اطلاعاتی درباره‌ی نوع واقعی خطا نداره، اما کلماتی
    # مثل "confirm" توش هست که باعث false-positive تو الگوهای زیر (مثلاً تشخیص
    # اشتباه "نیاز به تایید سن" برای یه خطای عمومی/ناشناخته) می‌شه. قبل از matching
    # حذفش می‌کنیم.
    low = re.sub(
        r"unexpected response from webpage request.*$",
        "",
        low,
        flags=re.DOTALL,
    )
    low = re.sub(r"please report this issue.*$", "", low, flags=re.DOTALL)
    low = re.sub(r"confirm you are on the latest version.*$", "", low, flags=re.DOTALL)

    if "sign in to confirm" in low or "confirm you're not a bot" in low:
        return "🤖 سایت درخواست تایید هویت/لاگین کرده (احتمالاً محدودیت ضدربات)."
    if "private video" in low or ("private" in low and "video" in low):
        return "🔒 این ویدیو خصوصیه و بدون لاگین قابل دانلود نیست."
    if "video unavailable" in low or "content isn't available" in low or "no longer available" in low:
        return "❌ این محتوا دیگه در دسترس نیست (حذف شده یا محدود شده)."
    if "unsupported url" in low:
        return "🚫 این لینک/سایت توسط ربات پشتیبانی نمی‌شه."
    if "instagram" in low and "http error 400" in low:
        return (
            "🍪 اینستاگرام درخواست رو رد کرد (400) - معمولاً یعنی کوکی سشن "
            "منقضی/نامعتبر شده یا این IP سرور رو محدود کرده. نیاز به کوکی تازه داره."
        )
    if "http error 404" in low:
        return "❌ محتوا پیدا نشد (404) - احتمالاً حذف شده."
    if "http error 403" in low:
        return "🚫 سرور مقصد درخواست رو رد کرد (403 Forbidden)."
    if "http error 429" in low or " 429" in low:
        return "⏳ سایت مقصد موقتاً محدودیت تعداد درخواست گذاشته. چند دقیقه دیگه امتحان کن."
    if "geo" in low and ("restrict" in low or "block" in low):
        return "🌍 این محتوا در منطقه‌ی جغرافیایی سرور ربات محدود شده (geo-restriction)."
    if "copyright" in low:
        return "©️ این محتوا به‌دلیل کپی‌رایت در دسترس نیست."
    if (
        re.search(r"\bage[\s-]?restrict", low)
        or re.search(r"\bage[\s-]?gat", low)
        or "confirm your age" in low
        or "verify your age" in low
        or "age verification" in low
        or "sign in to confirm your age" in low
    ):
        return "🔞 این محتوا نیاز به تایید سن داره که ربات بهش دسترسی نداره."
    if "timed out" in low or "timeout" in low:
        return "⏱ درخواست به سرور مقصد timeout شد."

    # این چندتا الگو قبلاً پوشش داده نشده بودن - اضافه‌شون کردم چون تو
    # لاگ‌های واقعی yt-dlp/ffmpeg هم دیده می‌شن و پیام «دلیل نامشخص»
    # پیش‌فرض براشون گمراه‌کننده‌ست:
    if "drm" in low or "protected content" in low:
        return "🔐 این محتوا با DRM محافظت شده و از هیچ ابزاری قابل دانلود نیست."
    if "no space left on device" in low:
        return "💾 دیسک سرور پره - این خطا ربطی به لینک تو نداره، لطفاً به ادمین اطلاع بده."
    if "ffmpeg not found" in low or ("ffmpeg" in low and "not found" in low):
        return "🛠 ابزار ffmpeg رو سرور نصب/پیدا نشد - این یه مشکل تنظیمات سرورە، نه لینک."
    if "unable to extract" in low or "unable to download webpage" in low:
        return (
            "🔧 ساختار این سایت اخیراً عوض شده و yt-dlp هنوز نتونسته باهاش وفق پیدا کنه. "
            "معمولاً طی چند روز با آپدیت yt-dlp حل می‌شه - بعداً دوباره امتحان کن."
        )
    if "this live event will begin in" in low or "premieres in" in low:
        return "🔴 این یه پخش زنده/پریمیر برنامه‌ریزی‌شده‌ست که هنوز شروع نشده."
    if "members-only" in low or "join this channel" in low:
        return "👥 این محتوا فقط برای اعضای ویژه‌ی کانال در دسترسه."

    return None
