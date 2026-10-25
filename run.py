"""
اجرای کل بات با یک دستور — بدون نیاز به باز کردن چند ترمینال.

این اسکریپت سه پردازش رو با هم بالا میاره:
    ۱) بات تلگرام (bot.main)
    ۲) ورکر Celery (پردازش دانلود/آپلود)
    ۳) Celery beat (زمان‌بند وظایف دوره‌ای مثل پاک‌سازی فایل‌های مؤقت)
و اگه قابلیت لینک مستقیم تو .env فعاله باشه (DIRECT_LINK_ENABLED=true):
    ۴) وب‌سرور لینک دانلود مستقیم (webserver.direct_link_server)

با Ctrl+C همه با هم و تمیز بسته می‌شن.

نکته: این اسکریپت فقط جایگزین «چند تا ترمینال باز کردن» می‌شه؛ Redis
(و در صورت استفاده از Postgres، دیتابیس) هنوز باید جدا در دسترس باشه
(مثلاً با `docker-compose up -d` یا نصب محلی).

استفاده:
    python run.py
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

PROCESSES = [
    ("bot", [sys.executable, "-m", "bot.main"]),
    ("worker", [
        sys.executable, "-m", "celery", "-A", "worker.celery_app", "worker",
        "--loglevel=info", "-Q", "download,upload,default",
        # pool پیش‌فرض (prefork) با asyncio.create_subprocess_exec که برای
        # اجرای yt-dlp استفاده می‌شه تداخل داره: هردو رو SIGCHLD رقابت
        # می‌کنن و Celery گاهی اشتباهی فکر می‌کنه خودِ worker مرده
        # (WorkerLostError: exitcode 0)، درحالی‌که فقط زیرپروسه‌ی yt-dlp
        # بوده. چون این سرور concurrency=1 داره (تک‌هسته)، سودی از fork
        # چندپروسه‌ای نمی‌بریم؛ pool=solo (بدون fork اصلاً) این تداخل رو
        # کامل حذف می‌کنه، بدون افت توان عملی.
        "--pool=solo",
    ]),
    ("beat", [
        sys.executable, "-m", "celery", "-A", "worker.celery_app", "beat",
        "--loglevel=info",
    ]),
]


def _build_process_list():
    """لیست پردازش‌ها با توجه به .env - وب‌سرور لینک مستقیم فقط وقتی بالا
    میاد که DIRECT_LINK_ENABLED فعاله (پیش‌فرض خاموش)."""
    processes = list(PROCESSES)
    try:
        from bot.config import settings

        if settings.DIRECT_LINK_ENABLED:
            processes.append((
                "directlink",
                [sys.executable, "-m", "webserver.direct_link_server"],
            ))
    except Exception as e:
        # تنظیمات لود نشد (مثلاً BOT_TOKEN خالیه) - بات به‌هرحال با خطای
        # واضح کرش می‌کنه و run.py دلیلش رو از لاگ نشون می‌ده؛ این‌جا نرم رد شو
        print(f"[run.py] خوندن تنظیمات برای چک DIRECT_LINK_ENABLED ناموفق بود: {e}")
    return processes


def main() -> int:
    procs = []
    log_files = []
    run_logs_dir = ROOT / "logs"
    run_logs_dir.mkdir(exist_ok=True)
    try:
        for name, cmd in _build_process_list():
            print(f"[run.py] در حال اجرای {name}: {' '.join(cmd)}")
            # خروجی/ارور هر پردازش رو جدا توی logs/run_<name>.log هم می‌نویسیم،
            # چون وقتی هر سه با هم توی یک ترمینال چاپ می‌کنن، لاگ کرش یکی‌شون
            # ممکنه لابه‌لای بقیه گم بشه یا اگه اسکریپت زود ببندتشون، اصلاً دیده نشه.
            log_path = run_logs_dir / f"run_{name}.log"
            log_file = open(log_path, "a", buffering=1)
            log_files.append(log_file)
            log_file.write(f"\n\n===== شروع اجرا: {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            procs.append((
                name,
                subprocess.Popen(cmd, cwd=ROOT, stdout=log_file, stderr=subprocess.STDOUT),
                log_path,
            ))

        # منتظر می‌مونیم تا یکی از پردازش‌ها تموم بشه (یعنی کرش کرده یا متوقف شده)
        while True:
            for name, proc, log_path in procs:
                code = proc.poll()
                if code is not None:
                    print(f"[run.py] پردازش «{name}» با کد {code} متوقف شد. در حال بستن بقیه...")
                    print(f"[run.py] برای دیدن دلیل کرش: tail -n 50 {log_path}")
                    return code
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[run.py] دریافت Ctrl+C — در حال بستن همه‌ی پردازش‌ها...")
        return 0
    finally:
        for name, proc, _log_path in procs:
            if proc.poll() is None:
                proc.terminate()
        for name, proc, _log_path in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                print(f"[run.py] «{name}» به موقع بسته نشد، به‌زور می‌بندیم (kill)")
                proc.kill()
        for f in log_files:
            f.close()


if __name__ == "__main__":
    sys.exit(main())
