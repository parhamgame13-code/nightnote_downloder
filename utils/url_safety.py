"""
فیلتر SSRF مشترک بین بات و ورکر — فقط stdlib، بدون هیچ وابستگی.

قبلاً این منطق فقط تو bot/main.py بود (`_is_url_safe_to_fetch`) و فقط اسکیم و
host رو چک می‌کرد. سه حفره داشت که همه این‌جا بسته شدن:

۱. فرم‌های عددیِ IP که `ipaddress.ip_address` پارس‌شون نمی‌کنه ولی httpx/مرورگرها
   resolve می‌کنن — مثل «2130706433» (اعشاری = 127.0.0.1)، «0x7f000001» (هگز)
   و «0177.0.0.1» (اکتال نقطه‌دار). قبلاً این‌ها به‌عنوان «دامنه» رد می‌شدن و
   درخواست به loopback می‌رفت.
۲. آدرس‌های IPv6 با آدرس IPv4 نگاشت‌شده (::ffff:127.0.0.1) که is_loopback
   خودِ IPv6 برای‌شون False می‌ده.
۳. چکِ فقط-رشته‌ایِ host بدون DNS: حالا به‌صورت پیش‌فرض host واقعاً resolve
   می‌شه (تو ترد جدا، چون getaddrinfo بلاکینگه) و اگه *هرکدوم* از IPهای
   نتیجه خصوصی/لوکال باشه، URL رد می‌شه — این هم DNS rebinding رو در لحظه‌ی
   ورودی می‌گیره. (TOCTOU بین این چک و fetch نهایی رو کامل نمی‌بنده، ولی
   حملات رایج رو پوشش می‌ده؛ برای دانلود فایل، هر hop ریدایرکت هم دوباره
   چک می‌شه.)
"""
import asyncio
import ipaddress
import socket
from urllib.parse import urlparse


def _ip_is_dangerous(ip: "ipaddress._BaseAddress") -> bool:
    """آیا این IP خصوصی/لوکال/رزروشه؟ (آدرس IPv4 نگاشت‌شده در IPv6 هم باز می‌شه)"""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def host_is_dangerous(host: str) -> bool:
    """چک رشته‌ایِ host بدون DNS — سریع و بدون I/O."""
    host = (host or "").lower().strip().rstrip(".")
    if not host:
        return True

    # hostnameهای شناخته‌شده‌ی خطرناک
    if host in ("localhost", "metadata.google.internal"):
        return True

    try:
        return _ip_is_dangerous(ipaddress.ip_address(host))
    except ValueError:
        pass

    # فرم‌های عددی غیراستاندارد که ip_address پارس‌شون نمی‌کنه ولی شبکه
    # resolve‌شون می‌کنه: اعشاری خالص (2130706433)، هگز با 0x (0x7f000001)
    # و فرم نقطه‌دارِ همه-عددی مثل اکتال (0177.0.0.1) یا کوتاه (127.1).
    stripped = host.replace(".", "")
    if not stripped:
        return True
    if stripped.isdigit():
        return True
    if stripped.startswith("0x") and all(c in "0123456789abcdef" for c in stripped[2:]):
        return True
    return False


async def url_is_safe_to_fetch(url: str, resolve_dns: bool = True) -> bool:
    """
    آیا fetch کردن این URL توسط بات/ورکر امنه؟

    - اسکیم فقط http/https
    - host نه مستقیماً IP خصوصی باشه، نه فرم عددی غیراستاندارد، نه localhost
    - اگه resolve_dns (پیش‌فرض): host واقعاً resolve می‌شه و اگه *یکی* از
      IPهای نتیجه خصوصی/لوکال بود، رد می‌شه (جلوی DNS rebinding در لحظه‌ی
      ورودی و hostnameهایی که به IP داخلی اشاره می‌کنن).

    resolve چک رشته‌ای رو شامل می‌شه (خیلی سریع‌تره)؛ خطای resolve هم یعنی
    رد شدن (دامنه‌ی ناموجود نباید به دانلود برسه).
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if parsed.scheme not in ("http", "https"):
        return False

    host = (parsed.hostname or "").lower()
    if host_is_dangerous(host):
        return False

    if resolve_dns:
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
        except Exception:
            return False
        if not infos:
            return False
        for info in infos:
            addr = (info[4] or ("",))[0]
            try:
                ip = ipaddress.ip_address(addr.split("%")[0])
            except ValueError:
                return False
            if _ip_is_dangerous(ip):
                return False

    return True
