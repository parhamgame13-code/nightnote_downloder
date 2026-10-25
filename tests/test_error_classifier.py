"""
تست‌های واحد برای utils/error_classifier.py

این تابع رو از worker/tasks.py جدا کردیم دقیقاً به همین دلیل: قبلش برای
تست کردنش باید کل worker/tasks.py (با وابستگی به Celery/DB/BOT_TOKEN)
import می‌شد. الان یه ماژول مستقله که فقط به httpx نیاز داره.

اجرا (از ریشه‌ی پروژه، بعد از `pip install -r requirements-dev.txt`):
    pytest tests/test_error_classifier.py -v
"""
import httpx
import pytest

from utils.error_classifier import classify_download_error


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.com/file.mp4")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("error", request=request, response=response)


# ---------------------------------------------------------------------------
# خطاهای HTTP (httpx.HTTPStatusError) - همون کیس واقعی که تو ربات دیدیم
# ---------------------------------------------------------------------------

def test_403_mentions_forbidden_and_ip_lock_theory():
    msg = classify_download_error(_http_status_error(403))
    assert "403" in msg
    assert "Forbidden" in msg


def test_404_mentions_not_found():
    msg = classify_download_error(_http_status_error(404))
    assert "404" in msg


def test_410_mentions_expired():
    msg = classify_download_error(_http_status_error(410))
    assert "410" in msg


def test_429_mentions_rate_limit():
    msg = classify_download_error(_http_status_error(429))
    assert "429" in msg


@pytest.mark.parametrize("status", [500, 502, 503])
def test_5xx_mentions_server_error_code(status):
    msg = classify_download_error(_http_status_error(status))
    assert str(status) in msg


def test_unmapped_status_code_still_returns_something_with_the_code():
    msg = classify_download_error(_http_status_error(418))
    assert "418" in msg


# ---------------------------------------------------------------------------
# خطاهای اتصال/timeout
# ---------------------------------------------------------------------------

def test_connect_timeout_classified():
    err = httpx.ConnectTimeout("timed out")
    assert classify_download_error(err) is not None


def test_read_timeout_classified():
    err = httpx.ReadTimeout("timed out")
    assert classify_download_error(err) is not None


def test_connect_error_classified_generic():
    err = httpx.ConnectError("Connection refused")
    msg = classify_download_error(err)
    assert msg is not None
    assert "SSL" not in msg  # نباید با پیام SSL قاطی بشه


def test_connect_error_with_ssl_in_message_gets_ssl_specific_reason():
    err = httpx.ConnectError("SSL: CERTIFICATE_VERIFY_FAILED")
    msg = classify_download_error(err)
    assert "SSL" in msg


# ---------------------------------------------------------------------------
# متن خام (stderr یت-دی‌ال‌پی) - رشته، نه اکسپشن
# ---------------------------------------------------------------------------

def test_private_video_text():
    assert classify_download_error("ERROR: Private video") is not None


def test_video_unavailable_text():
    assert classify_download_error("ERROR: [youtube] abc: Video unavailable") is not None


def test_unsupported_url_text():
    assert classify_download_error("ERROR: Unsupported URL: https://example.com") is not None


def test_http_error_404_in_stderr_text():
    assert classify_download_error("ERROR: unable to download video data: HTTP Error 404: Not Found") is not None


def test_sign_in_to_confirm_text():
    assert classify_download_error("Sign in to confirm you're not a bot") is not None


def test_unrecognized_text_returns_none_so_caller_falls_back():
    # یه پیام کاملاً ناشناخته - باید None برگرده تا caller از پیام
    # پیش‌فرض خودش استفاده کنه (نه این‌که یه پیام گمراه‌کننده بسازیم)
    assert classify_download_error("ERROR: something totally unrecognized happened") is None


def test_case_insensitive_matching():
    assert classify_download_error("VIDEO UNAVAILABLE") is not None
