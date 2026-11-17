"""
هویت وب، سشن دیتابیس، محدودیت نرخ و احراز هویت ادمین.

هویت وب — نکته‌ی معماری مهم:
کاربرهای وب هیچ لاگینی ندارن؛ هر مرورگر یه شناسه‌ی تصادفی (uuid) تو کوکی
`nn_web_id` می‌گیره. این شناسه با یه هش قطعی (SHA-256) به یه telegram_id
مصنوعیِ *منفی* نگاشت می‌شه:

    telegram_id = -(10**15 + (sha256(web_id)[:8 bytes] % 9*10**14))

چرا منفی؟ چون آی‌دی‌های واقعی تلگرام مثبتن و آی‌دی سوپرگروپ‌ها هم منفیِ
۱۳ رقمیِ با پیشوند 100- هستن؛ بازه‌ی وب (-۱ تا -۱.۹ کوادریلیون) با هیچ‌کدوم
تداخل نداره و از روی علامت/بازه می‌شه کاربر وب رو از کاربر تلگرام تشخیص
داد. چون نگاشت قطعی‌ه، همون مرورگر همیشه همون ردیف User رو می‌بینه و سقف
روزانه، بن، تنظیمات و تاریخچه دقیقاً مثل بات کار می‌کنن — روی همون جدول‌های
users و download_jobs. جاب‌های وب با chat_id=0 و message_id=None ساخته می‌شن
(چون چت/پیام تلگرامی در کار نیست) و هیچ‌وقت نباید وارد صف Celery بات بشن.
"""
import base64
import hashlib
import hmac
import secrets
import time
import uuid
from collections import defaultdict, deque
from typing import Optional

from fastapi import Cookie, Depends, HTTPException, Request
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from db.crud import UserCRUD
from db.models import User
from db.session import async_session_maker

WEB_COOKIE_NAME = "nn_web_id"
ADMIN_COOKIE_NAME = "nn_admin"

# بازه‌ی telegram_id مصنوعیِ کاربرهای وب (منفی، بدون تداخل با آی‌دی تلگرام)
_WEB_ID_BASE = 10**15
_WEB_ID_SPAN = 9 * 10**14

# کلید موقت امضای کوکی ادمین وقتی WEB_SECRET_KEY ست نشده (هر استارت عوض می‌شه)
_ephemeral_secret: Optional[str] = None


def synthetic_telegram_id(web_id: str) -> int:
    """نگاشت قطعیِ شناسه‌ی وب به telegram_id منفی (همون ورودی = همون خروجی)."""
    digest = hashlib.sha256(web_id.encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], "big") % _WEB_ID_SPAN
    return -(_WEB_ID_BASE + value)


def new_web_id() -> str:
    """شناسه‌ی وب تازه (uuid4)."""
    return str(uuid.uuid4())


def is_valid_web_id(value: Optional[str]) -> bool:
    """کوکی وب باید یه رشته‌ی کوتاهِ امن باشه، نه هر چیزی که کلاینت بفرسته."""
    if not value or len(value) > 128:
        return False
    return all(c.isalnum() or c in "-_" for c in value)


def get_web_id(request: Request) -> str:
    """
    شناسه‌ی وب از روی کوکی؛ اگه کوکی معتبر نباشه یه شناسه‌ی تازه برمی‌گردونه
    (میدلورِ اپ همون شناسه رو تو کوکی پاسخ ست می‌کنه).
    """
    raw = request.cookies.get(WEB_COOKIE_NAME)
    if is_valid_web_id(raw):
        return raw  # type: ignore[return-value]
    # میدلور شناسه‌ی تازه رو روی request.state گذاشته (اگه گذاشته باشه)
    state_id = getattr(request.state, "nn_web_id", None)
    if is_valid_web_id(state_id):
        return state_id
    return new_web_id()


async def get_db() -> AsyncSession:
    """سشن دیتابیس به‌ازای هر درخواست (قابل override تو تست‌ها)."""
    async with async_session_maker() as session:
        yield session


class WebIdentity:
    """هویت حل‌شده‌ی کاربر وب برای یه درخواست."""

    def __init__(self, web_id: str, user: User):
        self.web_id = web_id
        self.user = user

    @property
    def user_id(self) -> int:
        return self.user.id

    @property
    def telegram_id(self) -> int:
        return self.user.telegram_id


async def get_current_web_user(
    request: Request, session: AsyncSession = Depends(get_db)
) -> WebIdentity:
    """
    کاربر وب رو (بساز اگه نبود) برمی‌گردونه؛ اگه بن شده باشه 403 می‌ده.
    username وب رو روی شناسه‌ی کوتاه ست می‌کنیم تا تو پنل ادمین قابل
    تشخیص باشه (full_name هم «کاربر وب» می‌مونه).
    """
    web_id = get_web_id(request)
    telegram_id = synthetic_telegram_id(web_id)
    user = await UserCRUD.get_or_create(
        session,
        telegram_id=telegram_id,
        username=f"web_{web_id[:8]}",
        full_name="کاربر وب",
    )
    if user.is_banned:
        raise HTTPException(
            status_code=403,
            detail=f"دسترسی شما مسدود شده است.{' دلیل: ' + user.ban_reason if user.ban_reason else ''}",
        )
    return WebIdentity(web_id, user)


# ---------------------------------------------------------------------------
# محدودیت نرخ (درون‌حافظه، به‌ازای IP)
# ---------------------------------------------------------------------------

_rate_buckets: dict[str, deque] = defaultdict(deque)


def check_rate_limit(request: Request) -> None:
    """سقف WEB_RATE_LIMIT_PER_MINUTE درخواست در دقیقه به‌ازای هر IP."""
    limit = settings.WEB_RATE_LIMIT_PER_MINUTE
    if limit <= 0:
        return
    client = request.client.host if request.client else "unknown"
    now = time.monotonic()
    bucket = _rate_buckets[client]
    while bucket and now - bucket[0] > 60:
        bucket.popleft()
    if len(bucket) >= limit:
        raise HTTPException(
            status_code=429, detail="درخواست‌ها خیلی زیاد شد؛ یک دقیقه صبر کن و دوباره امتحان کن."
        )
    bucket.append(now)


# ---------------------------------------------------------------------------
# احراز هویت ادمین وب (کوکی امضاشده با HMAC، بدون وابستگی جدید)
# ---------------------------------------------------------------------------

ADMIN_SESSION_MAX_AGE_SECONDS = 12 * 3600


def _admin_secret() -> str:
    """کلید امضا: WEB_SECRET_KEY، وگرنه یه کلید موقتِ هر-استارت."""
    global _ephemeral_secret
    if settings.WEB_SECRET_KEY:
        return settings.WEB_SECRET_KEY
    if _ephemeral_secret is None:
        _ephemeral_secret = secrets.token_hex(32)
        logger.warning(
            "WEB_SECRET_KEY ست نشده؛ سشن‌های ادمین وب با کلید موقت امضا می‌شن "
            "و بعد از ری‌استارت باطل می‌شن."
        )
    return _ephemeral_secret


def admin_enabled() -> bool:
    """پنل ادمین وب فقط وقتی فعاله که ADMIN_PASSWORD ست شده باشه."""
    return bool(settings.ADMIN_PASSWORD)


def create_admin_token() -> str:
    """توکن سشن ادمین: base64(timestamp + '.' + HMAC(timestamp))."""
    issued = str(int(time.time()))
    signature = hmac.new(
        _admin_secret().encode("utf-8"), issued.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    raw = f"{issued}.{signature}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def verify_admin_token(token: Optional[str]) -> bool:
    """اعتبارسنجی توکن سشن ادمین (امضا + انقضا)."""
    if not token or not admin_enabled():
        return False
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        issued, signature = raw.split(".", 1)
        expected = hmac.new(
            _admin_secret().encode("utf-8"), issued.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False
        return (time.time() - int(issued)) < ADMIN_SESSION_MAX_AGE_SECONDS
    except Exception:
        return False


async def require_admin(
    nn_admin: Optional[str] = Cookie(default=None, alias=ADMIN_COOKIE_NAME),
) -> bool:
    """وابستگی FastAPI برای روت‌های ادمین: 401 اگه سشن معتبر نباشه."""
    if not admin_enabled():
        raise HTTPException(status_code=503, detail="پنل ادمین وب غیرفعال است (ADMIN_PASSWORD تنظیم نشده).")
    if not verify_admin_token(nn_admin):
        raise HTTPException(status_code=401, detail="نیاز به ورود ادمین است.")
    return True
