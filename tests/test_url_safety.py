"""
تست‌های واحد برای utils/url_safety.py (فیلتر SSRF مشترک بات/ورکر)

اجرا (از ریشه‌ی پروژه، بعد از `pip install pytest`):
    pytest tests/test_url_safety.py -v
"""
import asyncio

from utils.url_safety import host_is_dangerous, url_is_safe_to_fetch


def _safe(url: str) -> bool:
    """url_is_safe_to_fetch بدون DNS (برای تست‌های قطعی و بدون شبکه)"""
    return asyncio.run(url_is_safe_to_fetch(url, resolve_dns=False))


# ---------------------------------------------------------------------------
# host های خصوصی/لوکال
# ---------------------------------------------------------------------------

def test_loopback_and_private_ips():
    assert host_is_dangerous("localhost")
    assert host_is_dangerous("127.0.0.1")
    assert host_is_dangerous("10.0.0.5")
    assert host_is_dangerous("172.16.1.1")
    assert host_is_dangerous("192.168.1.1")
    assert host_is_dangerous("169.254.169.254")  # متادیتای ابری
    assert host_is_dangerous("::1")
    assert host_is_dangerous("0.0.0.0")


def test_ipv4_mapped_in_ipv6():
    # ::ffff:127.0.0.1 - خودِ IPv6.is_loopback برای‌ش False می‌ده
    assert host_is_dangerous("::ffff:127.0.0.1")


def test_numeric_encoded_ip_forms():
    # فرم‌هایی که ip_address پارس‌شون نمی‌کنه ولی شبکه resolve‌شون می‌کنه
    assert host_is_dangerous("2130706433")   # 127.0.0.1 به فرم اعشاری
    assert host_is_dangerous("0x7f000001")   # هگز
    assert host_is_dangerous("0177.0.0.1")   # اکتال نقطه‌دار
    assert host_is_dangerous("127.1")        # فرم کوتاه


def test_metadata_hostnames():
    assert host_is_dangerous("metadata.google.internal")


# ---------------------------------------------------------------------------
# دامنه‌های عمومی
# ---------------------------------------------------------------------------

def test_public_hostnames_are_safe():
    assert not host_is_dangerous("example.com")
    assert not host_is_dangerous("cdn.example.com")
    assert not host_is_dangerous("aparat.com")


def test_empty_host_is_dangerous():
    assert host_is_dangerous("")
    assert host_is_dangerous(None)


# ---------------------------------------------------------------------------
# URL کامل (اسکیم + host، بدون DNS)
# ---------------------------------------------------------------------------

def test_scheme_must_be_http_or_https():
    assert _safe("ftp://example.com/file") is False
    assert _safe("file:///etc/passwd") is False
    assert _safe("gopher://example.com/") is False


def test_private_ip_url_rejected():
    assert _safe("http://127.0.0.1:8081/x") is False
    assert _safe("http://169.254.169.254/latest/meta-data/") is False
    assert _safe("http://2130706433/x") is False
    assert _safe("http://0x7f000001/x") is False


def test_public_url_ok_without_dns():
    assert _safe("https://example.com/file.mp4") is True
    assert _safe("https://cdn.example.com/dl/file_a1b2.mp4?t=abc&s=1") is True
