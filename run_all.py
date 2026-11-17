"""
اجرای همه‌چیز با یک دستور: وب + بات تلگرام + ورکر Celery + beat.

این اسکریپت نسخه‌ی «همه‌کاره»ی run.py هست و اون رو خراب نمی‌کنه:
- وب (webapp.server) روی $PORT (پیش‌فرض 8000) — برای Render/Railway/Fly.io
  و هر PaaSای که یه پورت HTTP انتظار داره.
- بات تلگرام (bot.main) با polling — فقط وقتی BOT_TOKEN ست باشه.
- ورکر Celery + beat — برای دانلودهای بات (وب رانرِ خودش رو داره و به
  Celery نیاز نداره، ولی بات بدون ورکر کار نمی‌کنه).
- اگه REDIS_URL به localhost اشاره کنه و redis-server نصب باشه ولی چیزی
  روش جواب نده، یه redis-server توکار (embedded) هم بالا میاد تا دپلویِ
  تک‌کانتینری (Render و مشابه) بدون Redis جدا هم کار کنه.

فلگ‌ها (env):
    RUN_WEB=1      وب (پیش‌فرض روشن)
    RUN_BOT=1      بات (پیش‌فرض روشن، ولی بدون BOT_TOKEN خودکار رد می‌شه)
    RUN_WORKER=1   ورکر Celery (پیش‌فرض روشن)
    RUN_BEAT=1     بیت Celery (پیش‌فرض روشن)
    RUN_REDIS=1    ردیس توکار (پیش‌فرض روشن، فقط وقتی لازم باشه بالا میاد)

مثال‌ها:
    python run_all.py                                   # همه‌چیز
    RUN_BOT=0 RUN_WORKER=0 RUN_BEAT=0 python run_all.py # فقط وب (سرویس web تو compose)
    PORT=8000 python run_all.py                         # وب روی پورت مشخص

توجه: وب‌سرور جدا‌ی لینک مستقیم (webserver/direct_link_server.py) این‌جا
بالا نمیاد، چون خودِ وب‌اپ مسیر /dl/ رو روی همون پورت وب سرو می‌کنه.

مثل run.py، خروجی هر پردازش تو logs/run_<name>.log می‌ره و اگه هر کدوم
(مخصوصاً وب) بمیره، بقیه هم بسته می‌شن تا orchestrator (Render/Docker)
ری‌استارت کنه. SIGINT/SIGTERM هم تمیز هندل می‌شه (برای کانتینرها مهمه).
"""
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent


def _flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _redis_target() -> tuple[str, int]:
    """هاست/پورت Redis از روی REDIS_URL (پیش‌فرض localhost:6379)."""
    raw = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    try:
        parsed = urlparse(raw)
        host = parsed.hostname or "localhost"
        port = parsed.port or 6379
    except Exception:
        host, port = "localhost", 6379
    return host, port


def _tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _needs_embedded_redis() -> bool:
    """ردیس توکار فقط وقتی لازمه که REDIS_URL لوکال باشه و چیزی روش نباشه."""
    if not _flag("RUN_REDIS", True):
        return False
    if not _flag("RUN_WORKER", True) and not _flag("RUN_BEAT", True):
        return False  # فقط وب لازم نیست به Redis وصل بشه
    if not shutil.which("redis-server"):
        return False
    host, port = _redis_target()
    if host not in ("localhost", "127.0.0.1", "::1"):
        return False
    return not _tcp_reachable(host, port)


def _build_process_list() -> list[tuple[str, list[str]]]:
    processes: list[tuple[str, list[str]]] = []

    if _needs_embedded_redis():
        _host, port = _redis_target()
        print(f"[run_all] ردیس لوکال در دسترس نیست؛ redis-server توکار روی پورت {port} بالا میاد.")
        processes.append((
            "redis",
            ["redis-server", "--port", str(port), "--save", "", "--appendonly", "no",
             "--protected-mode", "no", "--daemonize", "no"],
        ))

    if _flag("RUN_WEB", True):
        processes.append(("web", [sys.executable, "-m", "webapp.server"]))

    if _flag("RUN_BOT", True):
        bot_token = os.environ.get("BOT_TOKEN", "")
        # BOT_TOKEN ممکنه فقط تو .env باشه، نه env پروسه؛ از Settings هم بخون
        if not bot_token:
            try:
                from bot.config import settings

                bot_token = settings.BOT_TOKEN or ""
            except Exception:
                bot_token = ""
        if bot_token:
            processes.append(("bot", [sys.executable, "-m", "bot.main"]))
        else:
            print("[run_all] BOT_TOKEN ست نشده؛ بات تلگرام رد شد (فقط وب/ورکر بالا میاد).")

    celery_worker_cmd = [
        sys.executable, "-m", "celery", "-A", "worker.celery_app", "worker",
        "--loglevel=info", "-Q", "download,upload,default",
        # pool=solo: مثل run.py — prefork با asyncio.create_subprocess_exec
        # (اجرای yt-dlp) روی SIGCHLD رقابت می‌کنه و WorkerLostError می‌ده.
        "--pool=solo",
    ]
    if _flag("RUN_WORKER", True):
        processes.append(("worker", celery_worker_cmd))
    if _flag("RUN_BEAT", True):
        processes.append((
            "beat",
            [sys.executable, "-m", "celery", "-A", "worker.celery_app", "beat", "--loglevel=info"],
        ))
    return processes


def main() -> int:
    procs: list[tuple[str, subprocess.Popen, Path]] = []
    log_files = []
    stop_requested = {"flag": False}

    def _on_signal(signum, _frame):
        print(f"\n[run_all] سیگنال {signum} دریافت شد — در حال بستن همه‌ی پردازش‌ها...")
        stop_requested["flag"] = True

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    run_logs_dir = ROOT / "logs"
    run_logs_dir.mkdir(exist_ok=True)
    processes = _build_process_list()
    if not processes:
        print("[run_all] هیچ پردازشی برای اجرا انتخاب نشده (همه‌ی RUN_* خاموشن).")
        return 0

    try:
        for name, cmd in processes:
            print(f"[run_all] در حال اجرای {name}: {' '.join(cmd)}")
            log_path = run_logs_dir / f"run_{name}.log"
            log_file = open(log_path, "a", buffering=1)
            log_files.append(log_file)
            log_file.write(f"\n\n===== شروع اجرا: {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            procs.append((
                name,
                subprocess.Popen(cmd, cwd=ROOT, stdout=log_file, stderr=subprocess.STDOUT),
                log_path,
            ))

        while not stop_requested["flag"]:
            for name, proc, log_path in procs:
                code = proc.poll()
                if code is not None:
                    print(f"[run_all] پردازش «{name}» با کد {code} متوقف شد. در حال بستن بقیه...")
                    print(f"[run_all] برای دیدن دلیل کرش: tail -n 50 {log_path}")
                    return code
            time.sleep(0.5)
        return 0
    finally:
        for name, proc, _log_path in procs:
            if proc.poll() is None:
                proc.terminate()
        for name, proc, _log_path in procs:
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                print(f"[run_all] «{name}» به موقع بسته نشد، به‌زور می‌بندیم (kill)")
                proc.kill()
        for f in log_files:
            f.close()


if __name__ == "__main__":
    sys.exit(main())
