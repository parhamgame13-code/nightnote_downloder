"""
پنل ادمین وب — آینه‌ی دستورات ادمین بات (/stats، /topsites، /userinfo،
/ban، /unban، /broadcast، /toggle، /maxsize، /texts) روی وب.

ورود با ADMIN_PASSWORD (env) و کوکی امضاشده‌ی HMAC انجام می‌شه. اگه
ADMIN_PASSWORD خالی باشه، کل پنل غیرفعاله (503).

نکته‌ی مهمِ broadcast: فقط به آی‌دی‌های مثبت تلگرام ارسال می‌شه. آی‌دی‌های
منفیِ مصنوعیِ کاربرهای وب هرگز نباید به Telegram API برسن.
"""
import asyncio
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from db.crud import BotConfigCRUD, DownloadJobCRUD, DownloadStatsCRUD, UserCRUD
from db.models import DownloadJob, User
from utils.texts import EDITABLE_TEXTS
from webapp.deps import (
    ADMIN_COOKIE_NAME,
    admin_enabled,
    create_admin_token,
    get_db,
    require_admin,
)

router = APIRouter(prefix="/api/admin")

# وضعیت broadcastهای در حال اجرا (درون‌حافظه)
_broadcasts: dict[str, dict] = {}
_broadcast_counter = 0


class LoginRequest(BaseModel):
    password: str = Field(..., min_length=1, max_length=256)


class BanRequest(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=500)


class ToggleRequest(BaseModel):
    enabled: bool


class BroadcastRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    target: str = Field(default="all", pattern="^(all|active7)$")


class TextUpdateRequest(BaseModel):
    key: str = Field(..., max_length=100)
    value: str = Field(default="", max_length=4000)


class MaxSizeRequest(BaseModel):
    mb: int = Field(..., ge=1, le=2000)


@router.post("/login")
async def login(payload: LoginRequest, request: Request, response: Response):
    """ورود ادمین با رمز (ADMIN_PASSWORD) + ست کردن کوکی سشن امضاشده."""
    if not admin_enabled():
        raise HTTPException(status_code=503, detail="پنل ادمین وب غیرفعال است (ADMIN_PASSWORD تنظیم نشده).")
    import hmac as _hmac

    if not _hmac.compare_digest(payload.password, settings.ADMIN_PASSWORD):
        raise HTTPException(status_code=401, detail="رمز ادمین اشتباه است.")
    token = create_admin_token()
    is_https = request.url.scheme == "https"
    response.set_cookie(
        ADMIN_COOKIE_NAME,
        token,
        max_age=12 * 3600,
        httponly=True,
        samesite="lax",
        secure=is_https,
        path="/",
    )
    return {"ok": True}


@router.post("/logout")
async def logout(response: Response):
    """خروج ادمین (پاک کردن کوکی سشن)."""
    response.delete_cookie(ADMIN_COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
async def me(_: bool = Depends(require_admin)):
    return {"ok": True, "admin": True}


@router.get("/stats")
async def stats(
    _: bool = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
):
    """آمار امروز + وضعیت سرویس (مثل /stats بات، به‌علاوه‌ی آمار وب)."""
    today = await DownloadStatsCRUD.get_today_stats(session)
    enabled = await BotConfigCRUD.get_bool(session, "bot_enabled", True)
    max_size = await BotConfigCRUD.get_int(session, "max_file_size_mb", settings.MAX_FILE_SIZE_MB)
    top_site = await DownloadJobCRUD.get_most_used_site_today(session)
    active = await DownloadJobCRUD.count_active(session)
    total_users = (await session.execute(select(func.count()).select_from(User))).scalar() or 0
    web_users = (
        await session.execute(
            select(func.count()).select_from(User).where(User.telegram_id < 0)
        )
    ).scalar() or 0
    disk_state = await BotConfigCRUD.get(session, "disk_space_state") or "ok"
    return {
        "bot_enabled": enabled,
        "disk_state": disk_state,
        "max_file_size_mb": max_size,
        "today": {
            "total_downloads": today.total_downloads if today else 0,
            "successful_downloads": today.successful_downloads if today else 0,
            "failed_downloads": today.failed_downloads if today else 0,
            "total_size_mb": round(today.total_size_mb, 1) if today else 0,
            "most_used_site": top_site,
        },
        "active_jobs": active,
        "total_users": total_users,
        "web_users": web_users,
        "telegram_users": total_users - web_users,
    }


@router.get("/topsites")
async def topsites(
    _: bool = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
):
    """پرکاربردترین سایت‌ها + دامنه‌های unknown (مثل /topsites بات)."""
    breakdown = await DownloadJobCRUD.get_site_breakdown(session)
    unknown = await DownloadJobCRUD.get_top_unknown_domains(session)
    return {
        "sites": [{"site": site, "count": count} for site, count in breakdown],
        "unknown_domains": [{"domain": domain, "count": count} for domain, count in unknown],
    }


@router.get("/users")
async def search_users(
    q: str = "",
    limit: int = 20,
    _: bool = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
):
    """
    جست‌وجوی کاربر: با آی‌دی عددی (مثبت تلگرام / منفی وب) یا بخشی از
    username/full_name. هر نتیجه خلاصه‌ی آمار + breakdown سایت‌هاش رو داره.
    """
    q = (q or "").strip()
    users: list[User] = []
    if q:
        try:
            telegram_id = int(q)
            user = await UserCRUD.get_by_id(session, telegram_id)
            if user:
                users = [user]
        except ValueError:
            stmt = (
                select(User)
                .where(
                    (User.username.ilike(f"%{q}%")) | (User.full_name.ilike(f"%{q}%"))
                )
                .order_by(User.created_at.desc())
                .limit(max(1, min(limit, 50)))
            )
            users = list((await session.execute(stmt)).scalars().all())
    else:
        stmt = select(User).order_by(User.created_at.desc()).limit(max(1, min(limit, 50)))
        users = list((await session.execute(stmt)).scalars().all())

    results = []
    for user in users:
        summary = await DownloadJobCRUD.get_user_summary(session, user.id)
        breakdown = await DownloadJobCRUD.get_user_site_breakdown(session, user.id)
        results.append(
            {
                "id": user.id,
                "telegram_id": user.telegram_id,
                "is_web": user.telegram_id < 0,
                "username": user.username,
                "full_name": user.full_name,
                "is_banned": bool(user.is_banned),
                "ban_reason": user.ban_reason,
                "is_admin": bool(user.is_admin),
                "created_at": user.created_at.isoformat() if user.created_at else None,
                "summary": summary,
                "sites": [{"site": site, "count": count} for site, count in breakdown],
            }
        )
    return {"users": results}


@router.post("/users/{telegram_id}/ban")
async def ban_user(
    telegram_id: int,
    payload: BanRequest,
    _: bool = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
):
    """بن کاربر (تلگرام یا وب) — مثل /ban بات."""
    user = await UserCRUD.get_by_id(session, telegram_id)
    if not user:
        raise HTTPException(status_code=404, detail="کاربر پیدا نشد.")
    await UserCRUD.ban(session, telegram_id, reason=payload.reason)
    return {"ok": True, "telegram_id": telegram_id, "is_banned": True}


@router.post("/users/{telegram_id}/unban")
async def unban_user(
    telegram_id: int,
    _: bool = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
):
    """رفع بن کاربر — مثل /unban بات."""
    user = await UserCRUD.get_by_id(session, telegram_id)
    if not user:
        raise HTTPException(status_code=404, detail="کاربر پیدا نشد.")
    await UserCRUD.unban(session, telegram_id)
    return {"ok": True, "telegram_id": telegram_id, "is_banned": False}


@router.get("/toggle")
async def get_toggle(
    _: bool = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
):
    enabled = await BotConfigCRUD.get_bool(session, "bot_enabled", True)
    return {"bot_enabled": enabled}


@router.post("/toggle")
async def set_toggle(
    payload: ToggleRequest,
    _: bool = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
):
    """روشن/خاموش کردن کلی سرویس (بات + وب) — مثل /toggle بات."""
    await BotConfigCRUD.set(session, "bot_enabled", "true" if payload.enabled else "false")
    return {"bot_enabled": payload.enabled}


async def _run_broadcast(broadcast_id: str, html_text: str, target: str) -> None:
    """
    ارسال پیام همگانی به کاربرهای تلگرام (فقط آی‌دی مثبت) با یه نمونه‌ی
    تازه‌ی Bot که فقط ارسال می‌کنه (بدون polling). وضعیت در _broadcasts
    نگه‌داری می‌شه تا فرانت‌اند polling کنه.
    """
    from aiogram import Bot
    from aiogram.exceptions import TelegramRetryAfter

    from bot.config import build_bot_session
    from db.session import async_session_maker

    status = _broadcasts[broadcast_id]
    try:
        async with async_session_maker() as session:
            if target == "active7":
                targets = await UserCRUD.get_active_telegram_ids_since(session, days=7)
            else:
                targets = await UserCRUD.get_all_telegram_ids(session)
        # فقط آی‌دی‌های واقعی تلگرام (مثبت) — آی‌دی‌های مصنوعی وب هرگز
        targets = [t for t in targets if t and t > 0]
        status["total"] = len(targets)
        status["state"] = "running"

        bot = Bot(token=settings.BOT_TOKEN, session=build_bot_session())
        try:
            for telegram_id in targets:
                try:
                    await bot.send_message(telegram_id, html_text, parse_mode="HTML")
                    status["sent"] += 1
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                    try:
                        await bot.send_message(telegram_id, html_text, parse_mode="HTML")
                        status["sent"] += 1
                    except Exception:
                        status["failed"] += 1
                except Exception:
                    status["failed"] += 1
                await asyncio.sleep(0.05)
        finally:
            await bot.session.close()
        status["state"] = "done"
    except Exception as e:
        logger.error(f"پیام همگانی وب ناموفق بود: {e}")
        status["state"] = "failed"
        status["error"] = "ارسال پیام همگانی با خطا مواجه شد (توکن بات یا اتصال تلگرام را بررسی کن)."


@router.post("/broadcast")
async def start_broadcast(
    payload: BroadcastRequest,
    _: bool = Depends(require_admin),
):
    """شروع پیام همگانی به کاربرهای تلگرام (غیرهمزمان؛ وضعیت با polling)."""
    global _broadcast_counter
    if not settings.BOT_TOKEN:
        raise HTTPException(status_code=503, detail="توکن بات تنظیم نشده؛ broadcast ممکن نیست.")
    _broadcast_counter += 1
    broadcast_id = f"bc_{_broadcast_counter}_{int(time.time())}"
    _broadcasts[broadcast_id] = {
        "id": broadcast_id, "state": "queued",
        "total": 0, "sent": 0, "failed": 0, "target": payload.target,
    }
    asyncio.create_task(_run_broadcast(broadcast_id, payload.text, payload.target))
    return {"id": broadcast_id, "state": "queued"}


@router.get("/broadcast/{broadcast_id}")
async def broadcast_status(
    broadcast_id: str,
    _: bool = Depends(require_admin),
):
    status = _broadcasts.get(broadcast_id)
    if not status:
        raise HTTPException(status_code=404, detail="این broadcast پیدا نشد.")
    return dict(status)


@router.get("/texts")
async def list_texts(
    _: bool = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
):
    """متن‌های قابل‌ویرایش بات (فقط کلیدهای واقعیِ utils/texts.py)."""
    items = []
    for key, spec in EDITABLE_TEXTS.items():
        custom = await BotConfigCRUD.get(session, key)
        items.append(
            {
                "key": key,
                "label": spec["label"],
                "default": spec["default"],
                "current": custom if custom else spec["default"],
                "customized": bool(custom),
                "hint": spec.get("hint"),
            }
        )
    return {"texts": items}


@router.post("/texts")
async def update_text(
    payload: TextUpdateRequest,
    _: bool = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
):
    """
    ویرایش یه متن بات. اگه value خالی باشه، تنظیم حذف می‌شه (برگشت به
    پیش‌فرض کد) — مثل رفتار /texts بات.
    """
    if payload.key not in EDITABLE_TEXTS:
        raise HTTPException(status_code=400, detail="این کلید متن وجود ندارد.")
    if not payload.value.strip():
        await BotConfigCRUD.delete(session, payload.key)
        return {"key": payload.key, "current": EDITABLE_TEXTS[payload.key]["default"], "customized": False}
    await BotConfigCRUD.set(
        session, payload.key, payload.value, label_fa=EDITABLE_TEXTS[payload.key]["label"]
    )
    return {"key": payload.key, "current": payload.value, "customized": True}


@router.get("/maxsize")
async def get_maxsize(
    _: bool = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
):
    value = await BotConfigCRUD.get_int(session, "max_file_size_mb", settings.MAX_FILE_SIZE_MB)
    return {"max_file_size_mb": value}


@router.post("/maxsize")
async def set_maxsize(
    payload: MaxSizeRequest,
    _: bool = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
):
    """تنظیم سقف حجم فایل (مثل /maxsize بات) — روی وب و بات هر دو اعمال می‌شه."""
    await BotConfigCRUD.set(
        session, "max_file_size_mb", str(payload.mb), label_fa="حداکثر حجم فایل (MB)"
    )
    return {"max_file_size_mb": payload.mb}
