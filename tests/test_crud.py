"""
تست‌های واحد برای db/crud.py — دیتابیس SQLite درون‌حافظه.

بدون Redis، بدون BOT_TOKEN، بدون شبکه: db.crud و db.models کاملاً مستقل‌ان
(اگه db/__init__.py را ببینی: عمداً eager import نداره تا همین بشه).
هر تست یه دیتابیس درون‌حافظه‌ی خودش رو می‌سازه (StaticPool تا همه‌ی
connectionها همین یک دیتابیس رو ببینن).

اجرا (از ریشه‌ی پروژه، بعد از `pip install -r requirements-dev.txt`):
    pytest tests/test_crud.py -v
"""
import asyncio
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from db.crud import DownloadJobCRUD, FileCacheCRUD, UserCRUD
from db.models import Base, DownloadStatus, FileType


async def _fresh_db():
    """دیتابیس درون‌حافظه‌ی تازه با جدول‌ها"""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    return engine, maker


def _run(coro):
    return asyncio.run(coro)


async def _make_user(session, telegram_id=111):
    return await UserCRUD.get_or_create(session, telegram_id=telegram_id, full_name="تستی")


async def _make_job(session, user_id, *, site="youtube", status=DownloadStatus.PENDING,
                    file_type=FileType.VIDEO, **kwargs):
    job = await DownloadJobCRUD.create(
        session, user_id=user_id, chat_id=-100123, url="https://example.com/x",
        detected_site=site, file_type=file_type, **kwargs,
    )
    if status != DownloadStatus.PENDING:
        await DownloadJobCRUD.update_status(session, job.id, status)
        job = await DownloadJobCRUD.get_by_id(session, job.id)
    return job


# ---------------------------------------------------------------------------
# claim_for_processing - قلب رفع مسابقه‌ی دابل‌کلیک
# ---------------------------------------------------------------------------

def test_claim_first_wins_and_second_fails():
    """دو ادعای پشت‌سرهم از status یکسان: اولی برنده، دومی باید False بگیره"""

    async def _main():
        engine, maker = await _fresh_db()
        async with maker() as session:
            user = await _make_user(session)
            job = await _make_job(session, user.id)

            first = await DownloadJobCRUD.claim_for_processing(
                session, job.id,
                from_statuses=(DownloadStatus.PENDING,),
                to_status=DownloadStatus.DETECTING,
                selected_quality="720p",
            )
            assert first is True

            # رکورد تازه بخون - status و فیلد باید اعمال شده باشن
            fresh = await DownloadJobCRUD.get_by_id(session, job.id)
            assert fresh.status == DownloadStatus.DETECTING
            assert fresh.selected_quality == "720p"

            second = await DownloadJobCRUD.claim_for_processing(
                session, job.id,
                from_statuses=(DownloadStatus.PENDING,),
                to_status=DownloadStatus.DETECTING,
            )
            assert second is False, "ادعای دوم از روی status تغییرکرده نباید موفق شه"
    _run(_main())


def test_claim_from_wrong_status_fails():
    async def _main():
        engine, maker = await _fresh_db()
        async with maker() as session:
            user = await _make_user(session)
            job = await _make_job(session, user.id, status=DownloadStatus.FAILED)

            ok = await DownloadJobCRUD.claim_for_processing(
                session, job.id,
                from_statuses=(DownloadStatus.PENDING, DownloadStatus.DETECTING),
                to_status=DownloadStatus.DOWNLOADING,
            )
            assert ok is False
            fresh = await DownloadJobCRUD.get_by_id(session, job.id)
            assert fresh.status == DownloadStatus.FAILED, "status نباید دست بخوره"
    _run(_main())


def test_retry_claim_resets_error_fields():
    """مسیر retry: FAILED → DOWNLOADING با ریست error_msg/finished_at"""

    async def _main():
        engine, maker = await _fresh_db()
        async with maker() as session:
            user = await _make_user(session)
            job = await _make_job(session, user.id, status=DownloadStatus.FAILED, error_msg="خطای قدیمی")

            ok = await DownloadJobCRUD.claim_for_processing(
                session, job.id,
                from_statuses=(DownloadStatus.PENDING, DownloadStatus.DETECTING, DownloadStatus.FAILED),
                to_status=DownloadStatus.DOWNLOADING,
                error_msg=None, finished_at=None,
            )
            assert ok is True
            fresh = await DownloadJobCRUD.get_by_id(session, job.id)
            assert fresh.status == DownloadStatus.DOWNLOADING
            assert fresh.error_msg is None
            assert fresh.finished_at is None
    _run(_main())


def test_set_celery_task_id_does_not_touch_status():
    """بعد از dispatch، فقط task_id ست بشه - statusِ DOWNLOADINGِ ورکر نباید
    به PENDING برگردونده بشه (باگی که قبلاً بود)"""

    async def _main():
        engine, maker = await _fresh_db()
        async with maker() as session:
            user = await _make_user(session)
            job = await _make_job(session, user.id, status=DownloadStatus.DOWNLOADING)

            await DownloadJobCRUD.set_celery_task_id(session, job.id, "task-abc")
            fresh = await DownloadJobCRUD.get_by_id(session, job.id)
            assert fresh.celery_task_id == "task-abc"
            assert fresh.status == DownloadStatus.DOWNLOADING, "status نباید عوض شه"
    _run(_main())


# ---------------------------------------------------------------------------
# update_status
# ---------------------------------------------------------------------------

def test_update_status_sets_started_and_finished_at():
    async def _main():
        engine, maker = await _fresh_db()
        async with maker() as session:
            user = await _make_user(session)
            job = await _make_job(session, user.id)
            assert job.started_at is None

            await DownloadJobCRUD.update_status(session, job.id, DownloadStatus.DOWNLOADING)
            fresh = await DownloadJobCRUD.get_by_id(session, job.id)
            assert fresh.started_at is not None
            assert fresh.finished_at is None

            await DownloadJobCRUD.update_status(session, job.id, DownloadStatus.DONE)
            fresh = await DownloadJobCRUD.get_by_id(session, job.id)
            assert fresh.finished_at is not None
    _run(_main())


# ---------------------------------------------------------------------------
# FileCacheCRUD - مسیر IntegrityError (مسابقه‌ی دو درخواست همزمان)
# ---------------------------------------------------------------------------

def test_filecache_integrity_error_falls_back_to_update():
    """دو درخواست همزمان برای همون لینک: هر دو select خالی می‌بینن، insert دوم
    UNIQUE می‌خوره. قبلاً کرش می‌کرد و job موفق رو FAILED ثبت می‌کرد؛ حالا
    باید rollback و آپدیت رکوردِ رقیب انجام شه. مسابقه رو با یه session
    رقیب که وسط commit ما insert می‌زنه شبیه‌سازی می‌کنیم."""

    async def _main():
        engine, maker = await _fresh_db()
        async with maker() as session:
            orig_commit = session.commit
            calls = {"n": 0}

            async def flaky_commit():
                calls["n"] += 1
                if calls["n"] == 1:
                    # شبیه‌سازی ورکر همزمان: رقیب دقیقاً قبل از شکستن commit ما
                    # همون رکورد رو insert و commit کرده
                    from db.models import FileCache
                    async with maker() as rival:
                        rival.add(FileCache(
                            url_hash="hash-1", source_url="https://example.com/x",
                            quality="best", telegram_file_id="file-rival",
                            file_type=FileType.VIDEO, file_name="rival.mp4",
                        ))
                        await rival.commit()
                    raise IntegrityError("INSERT", None, Exception("UNIQUE constraint failed"))
                return await orig_commit()

            session.commit = flaky_commit
            try:
                result = await FileCacheCRUD.create(
                    session, url_hash="hash-1", source_url="https://example.com/x",
                    quality="best", telegram_file_id="file-new", file_type=FileType.VIDEO,
                    file_name="new.mp4",
                )
            finally:
                session.commit = orig_commit

            assert result.telegram_file_id == "file-new"
            assert result.file_name == "new.mp4"

            # دقیقاً یک رکورد باید وجود داشته باشه (نه دوتا، نه صفر)
            from sqlalchemy import select, func
            from db.models import FileCache
            count = (await session.execute(
                select(func.count()).select_from(FileCache)
            )).scalar()
            assert count == 1
    _run(_main())


# ---------------------------------------------------------------------------
# شمارش روزانه و تفکیک آپلود/لینک
# ---------------------------------------------------------------------------

def test_count_today_by_user_uploads_split():
    async def _main():
        engine, maker = await _fresh_db()
        async with maker() as session:
            user = await _make_user(session)
            await _make_job(session, user.id, site="telegram_upload", status=DownloadStatus.DONE)
            await _make_job(session, user.id, site="youtube", status=DownloadStatus.DONE)
            await _make_job(session, user.id, site="youtube", status=DownloadStatus.FAILED)

            assert await DownloadJobCRUD.count_today_by_user(session, user.id) == 2
            assert await DownloadJobCRUD.count_today_by_user(session, user.id, only_uploads=True) == 1
            assert await DownloadJobCRUD.count_today_by_user(session, user.id, only_uploads=False) == 1
    _run(_main())


def test_count_today_excludes_other_days():
    async def _main():
        engine, maker = await _fresh_db()
        async with maker() as session:
            user = await _make_user(session)
            job = await _make_job(session, user.id, site="youtube", status=DownloadStatus.DONE)
            job.created_at = datetime.utcnow() - timedelta(days=2)
            await session.commit()

            assert await DownloadJobCRUD.count_today_by_user(session, user.id) == 0
    _run(_main())


def test_get_most_used_site_today_is_real_mode():
    """«پرکاربردترین» باید mode واقعی امروز باشه، نه آخرین سایت ثبت‌شده"""

    async def _main():
        engine, maker = await _fresh_db()
        async with maker() as session:
            user = await _make_user(session)
            for _ in range(2):
                await _make_job(session, user.id, site="youtube", status=DownloadStatus.DONE)
            await _make_job(session, user.id, site="spotify", status=DownloadStatus.DONE)

            old = await _make_job(session, user.id, site="instagram", status=DownloadStatus.DONE)
            old.created_at = datetime.utcnow() - timedelta(days=3)
            await session.commit()

            top = await DownloadJobCRUD.get_most_used_site_today(session)
            assert top == "youtube"
    _run(_main())


def test_get_most_used_site_empty():
    async def _main():
        engine, maker = await _fresh_db()
        async with maker() as session:
            assert await DownloadJobCRUD.get_most_used_site_today(session) is None
    _run(_main())


# ---------------------------------------------------------------------------
# نگه‌داری تاریخچه
# ---------------------------------------------------------------------------

def test_archive_old_jobs_deletes_only_old():
    async def _main():
        engine, maker = await _fresh_db()
        async with maker() as session:
            user = await _make_user(session)
            old = await _make_job(session, user.id, site="youtube", status=DownloadStatus.DONE)
            old.created_at = datetime.utcnow() - timedelta(days=100)
            await session.commit()
            fresh = await _make_job(session, user.id, site="youtube", status=DownloadStatus.DONE)

            deleted = await DownloadJobCRUD.archive_old_jobs(session, older_than_days=90)
            assert deleted == 1

            remaining = await DownloadJobCRUD.get_by_id(session, fresh.id)
            assert remaining is not None
            assert await DownloadJobCRUD.get_by_id(session, old.id) is None
    _run(_main())


def test_archive_old_jobs_zero_deletes_nothing():
    async def _main():
        engine, maker = await _fresh_db()
        async with maker() as session:
            user = await _make_user(session)
            await _make_job(session, user.id, site="youtube", status=DownloadStatus.DONE)
            assert await DownloadJobCRUD.archive_old_jobs(session, older_than_days=0) == 0
    _run(_main())


# ---------------------------------------------------------------------------
# کاربر
# ---------------------------------------------------------------------------

def test_increment_daily_count_resets_on_new_day():
    async def _main():
        engine, maker = await _fresh_db()
        async with maker() as session:
            user = await _make_user(session)
            user.daily_download_count = 5
            user.last_download_date = (datetime.utcnow() - timedelta(days=1)).date()
            await session.commit()

            await UserCRUD.increment_daily_count(session, user.telegram_id)
            fresh = await UserCRUD.get_by_id(session, user.telegram_id)
            assert fresh.daily_download_count == 1, "روز جدید باید شمارنده ریست شه"

            await UserCRUD.increment_daily_count(session, user.telegram_id)
            fresh = await UserCRUD.get_by_id(session, user.telegram_id)
            assert fresh.daily_download_count == 2
    _run(_main())


def test_get_or_create_updates_profile():
    async def _main():
        engine, maker = await _fresh_db()
        async with maker() as session:
            u1 = await _make_user(session, telegram_id=42)
            u2 = await _make_user(session, telegram_id=42)
            assert u1.id == u2.id, "دوبار get_or_create نباید دو رکورد بسازه"

            u3 = await UserCRUD.get_or_create(session, telegram_id=42, username="newname")
            assert u3.username == "newname"
    _run(_main())


def test_playlist_progress_counts():
    async def _main():
        engine, maker = await _fresh_db()
        async with maker() as session:
            user = await _make_user(session)
            for i, st in enumerate(
                [DownloadStatus.DONE, DownloadStatus.DONE,
                 DownloadStatus.FAILED, DownloadStatus.DOWNLOADING]
            ):
                await _make_job(
                    session, user.id, site="spotify", status=st,
                    playlist_id="batch-1", playlist_index=i + 1, playlist_total=4,
                )

            progress = await DownloadJobCRUD.get_playlist_progress(session, "batch-1")
            assert progress == {"total": 4, "done": 2, "failed": 1, "finished": 3}
    _run(_main())
