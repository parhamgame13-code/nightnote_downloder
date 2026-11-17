"""
تست‌های نسخه‌ی وب (webapp/) — بدون شبکه‌ی واقعی، بدون Redis، بدون BOT_TOKEN.

دیتابیس: SQLite درون‌حافظه با StaticPool که از طریق dependency override
جای سشن واقعی وب‌اپ می‌شینه (پس lifespan/startup اجرا نمی‌شه و فایل
bot.db واقعی دست نمی‌خوره). متادیتا و yt-dlp موک می‌شن.
"""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# Settings موقع import شدن BOT_TOKEN می‌خواد — تو تست‌ها یه مقدار ساختگی
# می‌ذاریم (هیچ درخواست واقعی تلگرام تو این تست‌ها زده نمی‌شه).
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN-DO-NOT-USE")
os.environ.setdefault("ADMIN_IDS", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from bot.config import settings
from db.models import Base
from webapp.app import create_app
from webapp.deps import get_db, synthetic_telegram_id
from webapp.downloader import QUALITY_FORMAT_MAP
from webapp.storage import resolve_served_file


# ---------------------------------------------------------------------------
# فیکسچر: اپ + دیتابیس درون‌حافظه
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient با دیتابیس تستی و پوشه‌ی فایل تستی."""
    monkeypatch.setattr(settings, "DIRECT_LINK_DIR", str(tmp_path / "direct_links"))
    monkeypatch.setattr(settings, "TEMP_DIR", str(tmp_path / "tmp"))

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    asyncio.run(_create_tables(engine))
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _test_db():
        async with maker() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = _test_db
    # TestClient رو بدون context manager می‌سازیم تا startup (init_db روی
    # دیتابیس واقعی + ساخت bot.db) اجرا نشه — override بالا سشن تستی رو می‌ده.
    c = TestClient(app, raise_server_exceptions=False)
    yield c
    asyncio.run(engine.dispose())


async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ---------------------------------------------------------------------------
# هویت وب: نگاشت قطعی uuid -> telegram_id منفی
# ---------------------------------------------------------------------------

def test_synthetic_telegram_id_deterministic_and_negative():
    web_id = "123e4567-e89b-12d3-a456-426614174000"
    first = synthetic_telegram_id(web_id)
    second = synthetic_telegram_id(web_id)
    assert first == second, "همون ورودی باید همیشه همون خروجی رو بده"
    assert first < 0, "آی‌دی وب باید منفی باشه تا با آی‌دی تلگرام قاطی نشه"
    assert first < -(10**15) + 1 or first <= -(10**15)
    assert -(10**15 + 9 * 10**14) <= first <= -(10**15)


def test_synthetic_telegram_id_differs_per_web_id():
    assert synthetic_telegram_id("aaaaaaaa-0000-0000-0000-000000000001") != synthetic_telegram_id(
        "bbbbbbbb-0000-0000-0000-000000000002"
    )


# ---------------------------------------------------------------------------
# quality map وب باید عین worker/tasks.py باشه
# ---------------------------------------------------------------------------

def test_quality_format_map_matches_worker():
    assert QUALITY_FORMAT_MAP["best"] == "bv*+ba/b"
    assert QUALITY_FORMAT_MAP["1080p"] == "bv*[height<=1080]+ba/b[height<=1080]"
    assert QUALITY_FORMAT_MAP["720p"] == "bv*[height<=720]+ba/b[height<=720]"
    assert QUALITY_FORMAT_MAP["480p"] == "bv*[height<=480]+ba/b[height<=480]"
    assert QUALITY_FORMAT_MAP["video_note"] == QUALITY_FORMAT_MAP["720p"]


# ---------------------------------------------------------------------------
# storage: جلوگیری از path traversal
# ---------------------------------------------------------------------------

def test_storage_traversal_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DIRECT_LINK_DIR", str(tmp_path / "direct_links"))
    with pytest.raises(ValueError):
        resolve_served_file("../secret", "file.mp4")
    with pytest.raises(ValueError):
        resolve_served_file("abc/def", "file.mp4")
    with pytest.raises(ValueError):
        resolve_served_file("goodtoken", "../etc/passwd")
    with pytest.raises(ValueError):
        resolve_served_file("goodtoken", "..%2fetc%2fpasswd")
    with pytest.raises(ValueError):
        resolve_served_file("", "file.mp4")


def test_storage_resolves_legit_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DIRECT_LINK_DIR", str(tmp_path / "direct_links"))
    token_dir = tmp_path / "direct_links" / "tok123"
    token_dir.mkdir(parents=True)
    target = token_dir / "video.mp4"
    target.write_bytes(b"fake")
    assert resolve_served_file("tok123", "video.mp4") == target.resolve()


# ---------------------------------------------------------------------------
# HTTP: healthz، صفحه‌ی اصلی، اعتبارسنجی
# ---------------------------------------------------------------------------

def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_index_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "NightNote" in resp.text


def test_inspect_validation_errors(client):
    # بدنه‌ی خالی -> 422 اعتبارسنجی FastAPI
    assert client.post("/api/inspect", json={}).status_code == 422
    # رشته‌ای که URL نیست -> 400
    resp = client.post("/api/inspect", json={"url": "not-a-url"})
    assert resp.status_code == 400
    # URL بدون اسکیم -> 400
    resp = client.post("/api/inspect", json={"url": "example.com/video"})
    assert resp.status_code == 400


def test_inspect_ssrf_private_ips_rejected(client):
    """آدرس‌های داخلی/خصوصی باید قبل از هر fetch رد بشن (بدون DNS)."""
    for bad_url in (
        "http://127.0.0.1:8000/admin",
        "http://127.0.0.1/secret.mp4",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.5/file.mp4",
        "http://192.168.1.1/file.mp4",
        "http://[::1]/file.mp4",
        "http://localhost:6379/file.mp4",
    ):
        resp = client.post("/api/inspect", json={"url": bad_url})
        assert resp.status_code == 400, f"{bad_url} باید رد می‌شد، ولی {resp.status_code} داد"


def test_inspect_youtube_with_mocked_metadata(client):
    """مسیر موفق inspect با موک MetadataExtractor (بدون شبکه)."""
    fake_meta = SimpleNamespace(
        title="Test Video",
        artist="Test Artist",
        album="Test Album",
        duration=123,
        thumbnail_url="https://example.com/thumb.jpg",
        description="desc",
    )
    with patch(
        "webapp.routes_public.MetadataExtractor.extract_from_url",
        new=AsyncMock(return_value=fake_meta),
    ), patch(
        # چک SSRF برای hostname واقعی DNS می‌خواد؛ تو تست، فقط مسیر موفق
        # رو می‌خوایم (رد کردن IP خصوصی تو تست جدا، بدون موک، چک می‌شه)
        "webapp.routes_public.url_is_safe_to_fetch",
        new=AsyncMock(return_value=True),
    ):
        resp = client.post(
            "/api/inspect",
            json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["site"] == "youtube"
    assert data["title"] == "Test Video"
    assert data["duration"] == 123
    assert data["is_playlist"] is False
    codes = [q["code"] for q in data["qualities"]]
    assert codes == ["best", "1080p", "720p", "480p", "audio", "video_note"]


def test_jobs_validation_errors(client):
    assert client.post("/api/jobs", json={}).status_code == 422
    resp = client.post("/api/jobs", json={"url": "not-a-url", "quality": "best"})
    assert resp.status_code == 400


def test_settings_roundtrip(client):
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    assert resp.json()["spoiler_effect"] is False

    resp = client.put(
        "/api/settings",
        json={"spoiler_effect": True, "timezone": "Asia/Tehran"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["spoiler_effect"] is True
    assert resp.json()["timezone"] == "Asia/Tehran"

    # منطقه‌ی زمانی نامعتبر باید رد بشه
    resp = client.put("/api/settings", json={"timezone": "Not/AZone"})
    assert resp.status_code == 400


def test_admin_disabled_without_password(client, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "")
    resp = client.post("/api/admin/login", json={"password": "whatever"})
    assert resp.status_code == 503


def test_admin_login_wrong_and_right_password(client, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "s3cret")
    resp = client.post("/api/admin/login", json={"password": "wrong"})
    assert resp.status_code == 401

    resp = client.post("/api/admin/login", json={"password": "s3cret"})
    assert resp.status_code == 200
    assert "nn_admin" in resp.headers.get("set-cookie", "")

    # با همون کوکی، /me باید ۲۰۰ بده
    resp = client.get("/api/admin/me")
    assert resp.status_code == 200


def test_admin_requires_auth(client, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "s3cret")
    resp = client.get("/api/admin/stats")
    assert resp.status_code == 401
