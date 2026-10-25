"""
عملیات CRUD
"""
from datetime import datetime, timedelta
from sqlalchemy import select, func, and_, update, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from db.models import (
    User, DownloadJob, FileCache, BotConfig, DownloadStatus,
    FileType, DownloadStats
)


class UserCRUD:
    """عملیات کاربر"""
    
    @staticmethod
    async def get_or_create(
        session: AsyncSession,
        telegram_id: int,
        username: str = None,
        full_name: str = None
    ) -> User:
        """دریافت یا ایجاد کاربر"""
        # جستجو
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        user = result.scalars().first()
        
        if user:
            # بروزرسانی اطلاعات
            if username:
                user.username = username
            if full_name:
                user.full_name = full_name
            await session.commit()
            return user
        
        # ایجاد کاربر جدید
        user = User(
            telegram_id=telegram_id,
            username=username,
            full_name=full_name or "کاربر"
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user
    
    @staticmethod
    async def get_by_pk(session: AsyncSession, user_id: int) -> User | None:
        """دریافت کاربر با کلید داخلی (users.id) - برخلاف get_by_id که با telegram_id کار می‌کنه"""
        stmt = select(User).where(User.id == user_id)
        result = await session.execute(stmt)
        return result.scalars().first()

    @staticmethod
    async def get_all_telegram_ids(session: AsyncSession) -> list[int]:
        """آیدی تلگرام همه‌ی کاربرها - برای پیام همگانی «به همه»"""
        stmt = select(User.telegram_id)
        result = await session.execute(stmt)
        return [row[0] for row in result.all()]

    @staticmethod
    async def get_active_telegram_ids_since(session: AsyncSession, days: int) -> list[int]:
        """آیدی تلگرام کاربرهایی که تو N روز اخیر حداقل یه دانلود زدن -
        برای پیام همگانی «فقط فعال‌های اخیر» (به‌جای کل کاربرها، حتی
        اونایی که ماه‌ها پیش یه‌بار امتحان کردن و رفتن)."""
        from db.models import DownloadJob
        cutoff = datetime.utcnow() - timedelta(days=days)
        stmt = (
            select(User.telegram_id)
            .join(DownloadJob, DownloadJob.user_id == User.id)
            .where(DownloadJob.created_at >= cutoff)
            .distinct()
        )
        result = await session.execute(stmt)
        return [row[0] for row in result.all()]

    @staticmethod
    async def update_settings(session: AsyncSession, telegram_id: int, **fields) -> User | None:
        """آپدیت تنظیمات شخصی کاربر (منوی /settings) - فقط فیلدهایی که پاس داده شدن"""
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        user = result.scalars().first()
        if not user:
            return None
        for key, value in fields.items():
            setattr(user, key, value)
        await session.commit()
        await session.refresh(user)
        return user

    @staticmethod
    async def get_by_id(session: AsyncSession, telegram_id: int) -> User | None:
        """دریافت کاربر با ID"""
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        return result.scalars().first()
    
    @staticmethod
    async def ban(session: AsyncSession, telegram_id: int, reason: str = None):
        """بن کاربر"""
        user = await UserCRUD.get_by_id(session, telegram_id)
        if user:
            user.is_banned = True
            user.ban_reason = reason
            await session.commit()
    
    @staticmethod
    async def unban(session: AsyncSession, telegram_id: int):
        """رفع بن کاربر"""
        user = await UserCRUD.get_by_id(session, telegram_id)
        if user:
            user.is_banned = False
            user.ban_reason = None
            await session.commit()
    
    @staticmethod
    async def increment_daily_count(session: AsyncSession, telegram_id: int):
        """افزایش تعداد دانلود روزانه"""
        user = await UserCRUD.get_by_id(session, telegram_id)
        if user:
            # UTC نه date.today() - همه‌ی زمان‌های ذخیره‌شده (created_at و ...)
            # utcnow هستن؛ «امروز» با تایم‌زون محلی سرور جور درنمی‌اومد
            today = datetime.utcnow().date()
            if user.last_download_date != today:
                user.daily_download_count = 1
                user.last_download_date = today
            else:
                user.daily_download_count += 1
            await session.commit()


class DownloadJobCRUD:
    """عملیات کار دانلود"""
    
    @staticmethod
    async def create(
        session: AsyncSession,
        user_id: int,
        chat_id: int,
        url: str,
        detected_site: str,
        file_type: FileType = FileType.VIDEO,
        **kwargs
    ) -> DownloadJob:
        """ایجاد کار دانلود جدید"""
        job = DownloadJob(
            user_id=user_id,
            chat_id=chat_id,
            url=url,
            detected_site=detected_site,
            file_type=file_type,
            **kwargs
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)
        return job
    
    @staticmethod
    async def get_by_id(session: AsyncSession, job_id: int) -> DownloadJob | None:
        """دریافت کار با ID"""
        stmt = select(DownloadJob).where(DownloadJob.id == job_id)
        result = await session.execute(stmt)
        return result.scalars().first()

    @staticmethod
    async def count_today_by_user(
        session: AsyncSession, user_id: int, only_uploads: bool | None = None
    ) -> int:
        """تعداد دانلودهایی که این کاربر امروز (از نیمه‌شب) درخواست داده
        دانلودهایی که شکست خوردن یا لغو شدن حساب نمی‌شن، چون تقصیر کاربر نبوده
        و نباید سهمیه‌ی روزانه‌ش رو بابتشون از دست بده.

        only_uploads:
            None  -> همه‌چیز (رفتار قدیمی، برای آمار/پروفایل کاربر)
            True  -> فقط «آپلود مستقیم» (فایلی که خودِ کاربر به بات فرستاده)
            False -> فقط «دانلود از لینک» (سایت‌های خارجی)
        این تفکیک برای سهمیه‌ی روزانه‌ی جداگانه‌ی این دو مسیر لازمه، چون
        منابع مصرفی‌شون (پهنای باند دانلود خارجی در مقابل فقط دیسک/کپی محلی)
        فرق می‌کنه.
        """
        # UTC نه local - همسو با created_at که با utcnow ذخیره می‌شه
        today_start = datetime.combine(datetime.utcnow().date(), datetime.min.time())
        conditions = [
            DownloadJob.user_id == user_id,
            DownloadJob.created_at >= today_start,
            DownloadJob.status.notin_([DownloadStatus.FAILED, DownloadStatus.CANCELLED]),
        ]
        if only_uploads is True:
            conditions.append(DownloadJob.detected_site == "telegram_upload")
        elif only_uploads is False:
            conditions.append(DownloadJob.detected_site != "telegram_upload")
        stmt = select(func.count()).select_from(DownloadJob).where(and_(*conditions))
        result = await session.execute(stmt)
        return result.scalar() or 0

    @staticmethod
    async def get_by_direct_link_token(session: AsyncSession, token: str) -> DownloadJob | None:
        """پیدا کردن job از روی توکن لینک مستقیم (برای تمدید/آمار/QR)"""
        stmt = select(DownloadJob).where(DownloadJob.direct_link_token == token)
        result = await session.execute(stmt)
        return result.scalars().first()

    @staticmethod
    async def set_direct_link(
        session: AsyncSession, job_id: int, token: str, url: str, expires_at: datetime
    ):
        """ذخیره‌ی توکن/URL/زمان‌انقضای لینک مستقیمِ تازه‌ساخته‌شده رو تو دیتابیس
        (قبلاً این‌ها فقط تو استکِ همون تسک celery بودن و بعد از اون گم می‌شدن -
        برای تمدید دستی و آمار بازدید باید جایی پایدار ذخیره بشن)"""
        job = await DownloadJobCRUD.get_by_id(session, job_id)
        if job:
            job.direct_link_token = token
            job.direct_link_url = url
            job.direct_link_expires_at = expires_at
            job.direct_link_hit_count = 0
            await session.commit()

    @staticmethod
    async def renew_direct_link(session: AsyncSession, job_id: int, new_expires_at: datetime):
        """تمدید دستیِ زمان انقضای لینک مستقیم (بدون نیاز به ساخت دوباره‌ی فایل)"""
        job = await DownloadJobCRUD.get_by_id(session, job_id)
        if job and job.direct_link_token:
            job.direct_link_expires_at = new_expires_at
            await session.commit()
        return job

    @staticmethod
    async def clear_direct_link(session: AsyncSession, job_id: int):
        """بعد از پاک‌سازی نهاییِ لینک مستقیم (منقضی شد و فایل پاک شد)، فیلدهاش
        رو خالی می‌کنه تا لینکِ مرده تو دیتابیس نمونه"""
        job = await DownloadJobCRUD.get_by_id(session, job_id)
        if job:
            job.direct_link_token = None
            job.direct_link_url = None
            job.direct_link_expires_at = None
            await session.commit()

    @staticmethod
    async def increment_direct_link_hit(session: AsyncSession, token: str) -> int | None:
        """افزایش شمارنده‌ی بازدید یه لینک مستقیم (وب‌سرور مستقل این رو صدا می‌زنه).
        برمی‌گردونه: تعداد جدید بازدید، یا None اگه توکن پیدا نشد."""
        job = await DownloadJobCRUD.get_by_direct_link_token(session, token)
        if not job:
            return None
        job.direct_link_hit_count = (job.direct_link_hit_count or 0) + 1
        await session.commit()
        return job.direct_link_hit_count
    
    @staticmethod
    async def update_status(
        session: AsyncSession,
        job_id: int,
        status: DownloadStatus,
        **kwargs
    ):
        """بروزرسانی وضعیت"""
        job = await DownloadJobCRUD.get_by_id(session, job_id)
        if job:
            job.status = status
            
            if status == DownloadStatus.DOWNLOADING and not job.started_at:
                job.started_at = datetime.utcnow()
            
            if status in (DownloadStatus.DONE, DownloadStatus.FAILED, DownloadStatus.CANCELLED):
                job.finished_at = datetime.utcnow()
            
            # بروزرسانی فیلدهای دیگر
            for key, value in kwargs.items():
                if hasattr(job, key):
                    setattr(job, key, value)

            await session.commit()

    @staticmethod
    async def claim_for_processing(
        session: AsyncSession,
        job_id: int,
        from_statuses: tuple,
        to_status: DownloadStatus,
        **fields,
    ) -> bool:
        """ادعای اتمیک یه جاب برای پردازش (compare-and-set روی status).

        UPDATE ... WHERE id=? AND status IN (from_statuses) — اگه rowcount صفر
        باشه یعنی یه کلیک/پردازش دیگه همزمان جاب رو قبلاً گرفته (یا لغو شده)،
        پس caller باید dispatch نکنه.

        چرا لازمه: قبلاً جریان «چک status تو حافظه + بعد update_status» بود که
        دو کلیکِ خیلی سریع روی دکمه‌ی کیفیت/تلاش‌دوباره هر دو از چک رد می‌شدن و
        دوبار download_file.dispatch می‌کردن (دوبار دانلود + دوبار پیام). با
        compare-and-set سطح دیتابیس، فقط اولین درخواست برنده می‌شه."""
        values = {"status": to_status, **fields}
        stmt = (
            update(DownloadJob)
            .where(DownloadJob.id == job_id, DownloadJob.status.in_(from_statuses))
            .values(**values)
        )
        result = await session.execute(stmt)
        await session.commit()
        return bool(result.rowcount)

    @staticmethod
    async def set_celery_task_id(session: AsyncSession, job_id: int, task_id: str):
        """فقط celery_task_id رو ست می‌کنه (بدون دست‌زدن به status).

        قبلاً بعد از .delay() یه update_status(PENDING, celery_task_id=...)
        زده می‌شد که اگه ورکر خیلی سریع status رو DOWNLOADING کرده بود،
        برگردوندنش به PENDING بود و شمارنده/نمایش وضعیت رو خراب می‌کرد."""
        stmt = (
            update(DownloadJob)
            .where(DownloadJob.id == job_id)
            .values(celery_task_id=task_id)
        )
        await session.execute(stmt)
        await session.commit()
    
    @staticmethod
    async def get_playlist_progress(session: AsyncSession, playlist_id: str) -> dict:
        """
        شمارش وضعیت job‌های یه پلی‌لیست/آلبوم (بر اساس playlist_id مشترک).
        برای گزارش پیشرفت تجمیعی «X از N دانلود شد، Y ناموفق» به کاربر - چون
        هر ترک یه job جدا با پیام وضعیت جداست و بدون این، هیچ‌جا خلاصه‌ی کل
        پلی‌لیست دیده نمی‌شد.
        """
        stmt = (
            select(DownloadJob.status, func.count())
            .where(DownloadJob.playlist_id == playlist_id)
            .group_by(DownloadJob.status)
        )
        result = await session.execute(stmt)
        counts = {status: count for status, count in result.all()}

        done = counts.get(DownloadStatus.DONE, 0)
        failed = counts.get(DownloadStatus.FAILED, 0) + counts.get(DownloadStatus.CANCELLED, 0)
        total = sum(counts.values())
        return {"total": total, "done": done, "failed": failed, "finished": done + failed}

    @staticmethod
    async def count_active(session: AsyncSession) -> int:
        """تعداد جاب‌هایی که الان تو صف/در حال پردازشن (برای نمایش جایگاه صف به کاربر)"""
        stmt = select(func.count()).select_from(DownloadJob).where(
            DownloadJob.status.in_([DownloadStatus.PENDING, DownloadStatus.DETECTING, DownloadStatus.DOWNLOADING, DownloadStatus.UPLOADING])
        )
        result = await session.execute(stmt)
        return result.scalar() or 0

    @staticmethod
    async def get_most_used_site_today(session: AsyncSession) -> str | None:
        """پرکاربردترین سایت امروز (mode واقعی از روی jobهای امروز).

        قبلاً ستون most_used_site با هر دانلود overwrite می‌شد - یعنی چیزی که
        «محبوب‌ترین» نمایش داده می‌شد در واقع «آخرین سایت» بود. این query
        مستقیم واقعیت رو از جدول download_jobs می‌خونه."""
        today_start = datetime.combine(datetime.utcnow().date(), datetime.min.time())
        stmt = (
            select(DownloadJob.detected_site, func.count())
            .where(DownloadJob.created_at >= today_start)
            .group_by(DownloadJob.detected_site)
            .order_by(func.count().desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        row = result.first()
        return row[0] if row else None

    @staticmethod
    async def get_site_breakdown(session: AsyncSession, limit: int = 15) -> list[tuple[str, int]]:
        """پرکاربردترین سایت‌ها (همه‌ی زمان‌ها) - برای پنل آماری ادمین"""
        stmt = (
            select(DownloadJob.detected_site, func.count())
            .group_by(DownloadJob.detected_site)
            .order_by(func.count().desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return result.all()

    @staticmethod
    async def get_top_unknown_domains(session: AsyncSession, limit: int = 15) -> list[tuple[str, int]]:
        """دامنه‌های خامی که detected_site براشون "unknown" شده - تا بشه فهمید
        کدوم سایت‌ها بیشتر لازمه به LinkDetector اضافه بشن."""
        stmt = (
            select(DownloadJob.raw_domain, func.count())
            .where(DownloadJob.detected_site == "unknown", DownloadJob.raw_domain.isnot(None))
            .group_by(DownloadJob.raw_domain)
            .order_by(func.count().desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return result.all()

    @staticmethod
    async def get_user_site_breakdown(session: AsyncSession, user_pk: int, limit: int = 15) -> list[tuple[str, int]]:
        """سایت‌هایی که یه کاربر خاص ازشون استفاده کرده - برای پنل «این کاربر چیکار کرده»"""
        stmt = (
            select(DownloadJob.detected_site, func.count())
            .where(DownloadJob.user_id == user_pk)
            .group_by(DownloadJob.detected_site)
            .order_by(func.count().desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return result.all()

    @staticmethod
    async def fail_stale_jobs(session: AsyncSession, older_than_minutes: int = 120) -> int:
        """جاب‌هایی که به‌خاطر کرش worker یا گم‌شدن تسک تو Redis، برای مدت
        طولانی تو PENDING/DETECTING/DOWNLOADING/UPLOADING گیر کردن رو FAILED
        می‌کنه. بدون این، این جاب‌های "زامبی" برای همیشه تو دیتابیس
        active می‌مونن و شمارنده‌ی صف (count_active) رو برای همیشه اشتباه
        بالا نگه می‌دارن."""
        cutoff = datetime.utcnow() - timedelta(minutes=older_than_minutes)
        stmt = (
            select(DownloadJob)
            .where(
                DownloadJob.status.in_([
                    DownloadStatus.PENDING, DownloadStatus.DETECTING,
                    DownloadStatus.DOWNLOADING, DownloadStatus.UPLOADING,
                ]),
                DownloadJob.created_at < cutoff,
            )
        )
        result = await session.execute(stmt)
        stale_jobs = result.scalars().all()
        for job in stale_jobs:
            job.status = DownloadStatus.FAILED
            job.error_msg = "تایم‌اوت - این دانلود بیش از حد معمول طول کشید و به‌صورت خودکار لغو شد"
            job.finished_at = datetime.utcnow()
        if stale_jobs:
            await session.commit()
        return len(stale_jobs)

    @staticmethod
    async def get_pending_jobs(session: AsyncSession, limit: int = 10) -> list[DownloadJob]:
        """دریافت کارهای منتظر"""
        stmt = (
            select(DownloadJob)
            .where(DownloadJob.status == DownloadStatus.PENDING)
            .order_by(DownloadJob.created_at)
            .limit(limit)
        )
        result = await session.execute(stmt)
        return result.scalars().all()
    
    @staticmethod
    async def archive_old_jobs(session: AsyncSession, older_than_days: int = 90) -> int:
        """حذف jobهای قدیمی‌تر از N روز (نگه‌داری تاریخچه).

        بدون این، download_jobs بی‌نهایت رشد می‌کنه و SQLite به‌مرور کند
        می‌شه (هر پیام لینک یه INSERT و چند SELECT روی همین جدول می‌زنه).
        آمار روزانه (DownloadStats) و کش فایل‌ها دست‌نخورده می‌مونن؛ پوشه‌های
        لینک مستقیمِ یتیم رو sweep مبتنی بر mtime در cleanup_temp_files پاک
        می‌کنه پس لازم نیست اینجا سراغ فایل‌سیستم بریم."""
        # دفاعی: صفر/منفی یعنی غیرفعال - وگرنه cutoff=همین‌حالا یعنی حذفِ همه!
        if older_than_days <= 0:
            return 0
        cutoff = datetime.utcnow() - timedelta(days=older_than_days)
        stmt = delete(DownloadJob).where(DownloadJob.created_at < cutoff)
        result = await session.execute(stmt)
        await session.commit()
        return result.rowcount or 0

    @staticmethod
    async def get_user_history(
        session: AsyncSession,
        user_id: int,
        limit: int = 50
    ) -> list[DownloadJob]:
        """تاریخچه دانلودهای کاربر"""
        stmt = (
            select(DownloadJob)
            .where(DownloadJob.user_id == user_id)
            .order_by(DownloadJob.created_at.desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return result.scalars().all()

    @staticmethod
    async def get_user_summary(session: AsyncSession, user_id: int) -> dict:
        """خلاصه‌ی آمار یک کاربر (کل + امروز + موفق/ناموفق)"""
        total_stmt = select(func.count()).select_from(DownloadJob).where(
            DownloadJob.user_id == user_id
        )
        success_stmt = select(func.count()).select_from(DownloadJob).where(
            and_(DownloadJob.user_id == user_id, DownloadJob.status == DownloadStatus.DONE)
        )
        total = (await session.execute(total_stmt)).scalar() or 0
        success = (await session.execute(success_stmt)).scalar() or 0
        today = await DownloadJobCRUD.count_today_by_user(session, user_id)
        return {"total": total, "success": success, "today": today}


class FileCacheCRUD:
    """عملیات کش فایل"""

    @staticmethod
    async def get_by_hash(session: AsyncSession, url_hash: str) -> FileCache | None:
        """دریافت کش با hash"""
        stmt = select(FileCache).where(FileCache.url_hash == url_hash)
        result = await session.execute(stmt)
        cache = result.scalars().first()
        
        if cache:
            # بروزرسانی آمار
            cache.hit_count += 1
            cache.last_used_at = datetime.utcnow()
            await session.commit()
        
        return cache
    
    @staticmethod
    async def create(
        session: AsyncSession,
        url_hash: str,
        source_url: str,
        quality: str,
        telegram_file_id: str,
        file_type: FileType,
        file_name: str,
        **kwargs
    ) -> FileCache:
        """ایجاد یا بروزرسانی کش (upsert) — اگه لینک قبلاً کش شده باشه،
        به‌جای insert (که با UNIQUE constraint خطا می‌ده)، رکورد موجود رو آپدیت می‌کنه"""
        stmt = select(FileCache).where(FileCache.url_hash == url_hash)
        result = await session.execute(stmt)
        cache = result.scalars().first()

        if cache:
            cache.telegram_file_id = telegram_file_id
            cache.file_name = file_name
            cache.last_used_at = datetime.utcnow()
            for key, value in kwargs.items():
                if hasattr(cache, key):
                    setattr(cache, key, value)
            await session.commit()
            await session.refresh(cache)
            return cache

        cache = FileCache(
            url_hash=url_hash,
            source_url=source_url,
            quality=quality,
            telegram_file_id=telegram_file_id,
            file_type=file_type,
            file_name=file_name,
            **kwargs
        )
        session.add(cache)
        try:
            await session.commit()
        except IntegrityError:
            # مسابقه‌ی دو درخواست همزمان برای همون لینک: هر دو select خالی
            # می‌بینن و هر دو insert می‌زنن؛ دومی UNIQUE violation می‌خوره.
            # قبلاً همین IntegrityError بالا می‌رفت و کل تسک (که فایل رو با
            # موفقیت فرستاده بود) FAILED ثبت می‌شد. rollback و آپدیت رکورد
            # موجود به‌جای insert.
            await session.rollback()
            result = await session.execute(select(FileCache).where(FileCache.url_hash == url_hash))
            existing = result.scalars().first()
            if not existing:
                raise
            existing.telegram_file_id = telegram_file_id
            existing.file_name = file_name
            existing.last_used_at = datetime.utcnow()
            for key, value in kwargs.items():
                if hasattr(existing, key):
                    setattr(existing, key, value)
            await session.commit()
            await session.refresh(existing)
            return existing
        await session.refresh(cache)
        return cache

    @staticmethod
    async def clear_all(session: AsyncSession) -> int:
        """پاک کردن کامل کش (همه‌ی رکوردها) - بعدش همه‌ی لینک‌ها دوباره از صفر دانلود می‌شن"""
        from sqlalchemy import delete
        stmt = delete(FileCache)
        result = await session.execute(stmt)
        await session.commit()
        return result.rowcount

    @staticmethod
    async def clear_by_type(session: AsyncSession, file_type: FileType) -> int:
        """پاک کردن کش فقط برای یه نوع فایل خاص (مثلاً فقط ویدیوها)"""
        from sqlalchemy import delete
        stmt = delete(FileCache).where(FileCache.file_type == file_type)
        result = await session.execute(stmt)
        await session.commit()
        return result.rowcount


class BotConfigCRUD:
    """عملیات تنظیمات بات"""
    
    @staticmethod
    async def get(session: AsyncSession, key: str) -> str | None:
        """دریافت تنظیم"""
        stmt = select(BotConfig).where(BotConfig.key == key)
        result = await session.execute(stmt)
        config = result.scalars().first()
        return config.value if config else None
    
    @staticmethod
    async def get_int(session: AsyncSession, key: str, default: int = 0) -> int:
        """دریافت تنظیم عددی"""
        value = await BotConfigCRUD.get(session, key)
        if value:
            try:
                return int(value)
            except (ValueError, TypeError):
                pass
        return default
    
    @staticmethod
    async def get_bool(session: AsyncSession, key: str, default: bool = False) -> bool:
        """دریافت تنظیم بولی"""
        value = await BotConfigCRUD.get(session, key)
        if value:
            return value.lower() in ('true', '1', 'yes')
        return default
    
    @staticmethod
    async def get_list(session: AsyncSession, key: str, default: list = None) -> list:
        """دریافت تنظیم لیستی"""
        value = await BotConfigCRUD.get(session, key)
        if value:
            return [x.strip() for x in value.split(',') if x.strip()]
        return default or []
    
    @staticmethod
    async def set(
        session: AsyncSession,
        key: str,
        value: str,
        label_fa: str = None,
        updated_by: int = None
    ):
        """تنظیم کردن"""
        stmt = select(BotConfig).where(BotConfig.key == key)
        result = await session.execute(stmt)
        config = result.scalars().first()
        
        if config:
            config.value = value
            config.updated_by = updated_by
            config.updated_at = datetime.utcnow()
        else:
            config = BotConfig(
                key=key,
                value=value,
                label_fa=label_fa,
                updated_by=updated_by
            )
            session.add(config)
        
        await session.commit()

    @staticmethod
    async def delete(session: AsyncSession, key: str):
        """حذف یک تنظیم (برگردوندن به مقدار پیش‌فرض کد)"""
        from sqlalchemy import delete as sa_delete
        stmt = sa_delete(BotConfig).where(BotConfig.key == key)
        await session.execute(stmt)
        await session.commit()


class DownloadStatsCRUD:
    """عملیات آمار"""
    
    @staticmethod
    async def get_today_stats(session: AsyncSession) -> DownloadStats | None:
        """آمار امروز"""
        today = datetime.utcnow().date()
        stmt = select(DownloadStats).where(DownloadStats.date == today)
        result = await session.execute(stmt)
        return result.scalars().first()
    
    @staticmethod
    async def increment_today(
        session: AsyncSession,
        successful: bool = True,
        size_mb: float = 0,
        site: str = None
    ):
        """افزایش آمار امروز"""
        # UTC نه local - همسو با created_at که با utcnow ذخیره می‌شه
        today = datetime.utcnow().date()

        stmt = select(DownloadStats).where(DownloadStats.date == today)
        result = await session.execute(stmt)
        stats = result.scalars().first()
        
        if not stats:
            stats = DownloadStats(
                date=today,
                total_downloads=0,
                successful_downloads=0,
                failed_downloads=0,
                total_size_mb=0.0,
                total_users=0,
            )
            session.add(stats)
        
        stats.total_downloads += 1
        stats.total_size_mb += size_mb
        
        if successful:
            stats.successful_downloads += 1
        else:
            stats.failed_downloads += 1
        
        if site:
            stats.most_used_site = site
        
        await session.commit()
