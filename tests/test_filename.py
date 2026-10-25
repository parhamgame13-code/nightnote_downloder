"""
تست‌های واحد برای utils/filename.py - پاک‌سازی اسم فایل در سه سناریو

اجرا: python -m pytest tests/test_filename.py -v
"""
from utils.filename import safe_upload_filename, sanitize_ascii_filename, sanitize_remote_filename


# ---------------------------------------------------------------------------
# sanitize_ascii_filename - اسم دلخواه کاربر برای لینک مستقیم
# ---------------------------------------------------------------------------

def test_ascii_name_kept_and_normalized():
    assert sanitize_ascii_filename("my video") == "my_video"
    assert sanitize_ascii_filename("My.File-Name_2") == "My.File-Name_2"
    assert sanitize_ascii_filename("  spaced  name  ") == "spaced_name"


def test_ascii_name_strips_dangerous_chars():
    # کاراکترهای خاص حذف می‌شن، نه اینکه مسیر بسازن
    assert sanitize_ascii_filename('a"b/c\\d') == "abcd"
    assert sanitize_ascii_filename("file!.mp4") == "file.mp4"


def test_persian_or_empty_rejected():
    assert sanitize_ascii_filename("ویدیوی من") is None
    assert sanitize_ascii_filename("") is None
    assert sanitize_ascii_filename("   ") is None
    assert sanitize_ascii_filename("!!!") is None
    assert sanitize_ascii_filename(None) is None


def test_ascii_name_length_capped():
    out = sanitize_ascii_filename("a" * 250)
    assert out is not None and len(out) == 100


# ---------------------------------------------------------------------------
# sanitize_remote_filename - اسم Content-Disposition سرور مقصد (path traversal)
# ---------------------------------------------------------------------------

def test_path_traversal_neutralized():
    # سناریوی واقعی حمله: هدر filename="../../etc/cron.d/evil"
    assert sanitize_remote_filename("../../etc/cron.d/evil") == "evil"
    assert sanitize_remote_filename("..\\..\\windows\\evil.exe") == "evil.exe"


def test_query_fragment_and_dot_leading_stripped():
    assert sanitize_remote_filename("video.mp4?t=abc&s=1") == "video.mp4"
    assert sanitize_remote_filename("clip.mkv#t=30") == "clip.mkv"
    assert sanitize_remote_filename("..hidden.mp4") == "hidden.mp4"
    assert sanitize_remote_filename("..") == ""
    assert sanitize_remote_filename(".") == ""


def test_remote_name_nul_and_empty():
    assert sanitize_remote_filename("bad\x00name.mp4") == "badname.mp4"
    assert sanitize_remote_filename("") == ""
    assert sanitize_remote_filename(None) == ""


def test_remote_name_length_capped():
    out = sanitize_remote_filename("b" * 500)
    assert len(out) == 200


# ---------------------------------------------------------------------------
# safe_upload_filename - اسم فایل آپلودی کاربر
# ---------------------------------------------------------------------------

def test_upload_name_unsafe_chars_become_underscores():
    assert safe_upload_filename("a/b\\c.txt") == "a_b_c.txt"
    out = safe_upload_filename("ویدیو من.mp4")
    # هیچ کاراکتر غیرمجازی نمی‌مونه (فارسی → underscore)
    assert set(out) <= set("A-Za-z0-9._- ") or all(
        c in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._- " for c in out
    )
    assert out.endswith(".mp4")


def test_upload_name_length_capped_and_empty():
    assert len(safe_upload_filename("x" * 500)) == 150
    assert safe_upload_filename("") == ""
    assert safe_upload_filename(None) == ""
