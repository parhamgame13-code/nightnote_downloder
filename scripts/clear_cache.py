"""
اسکریپت یک‌باره برای پاک کردن کامل کش فایل‌ها (جدول file_cache).

بعد از پاک شدن، همه‌ی لینک‌ها دفعه‌ی بعد که فرستاده بشن دوباره از صفر
دانلود می‌شن (نه از کش) - یعنی ویدیوهای قدیمی هم این‌بار با تامبنیل
درست ساخته و ارسال می‌شن.

استفاده (از مسیر ریشه‌ی پروژه):
    python scripts/clear_cache.py            # فقط ویدیوها رو پاک می‌کنه
    python scripts/clear_cache.py --all      # کل کش (ویدیو + صوت + فایل) رو پاک می‌کنه
    python scripts/clear_cache.py --yes      # بدون سوال تاییدی، مستقیم اجرا کن
"""
import sys
import asyncio
import argparse
from pathlib import Path

# اضافه کردن ریشه‌ی پروژه به sys.path تا import ها کار کنن
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.session import async_session_maker
from db.crud import FileCacheCRUD
from db.models import FileType


async def main(clear_all: bool, skip_confirm: bool):
    scope = "کل کش (ویدیو + صوت + فایل)" if clear_all else "فقط کش ویدیوها"

    if not skip_confirm:
        answer = input(f"⚠️  {scope} پاک می‌شه و برگشت‌پذیر نیست. مطمئنی؟ (yes/no): ")
        if answer.strip().lower() not in ("yes", "y"):
            print("لغو شد.")
            return

    async with async_session_maker() as session:
        if clear_all:
            count = await FileCacheCRUD.clear_all(session)
        else:
            count = await FileCacheCRUD.clear_by_type(session, FileType.VIDEO)

    print(f"✅ {count} ردیف از کش پاک شد.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="پاک کردن کش فایل‌های ربات")
    parser.add_argument("--all", action="store_true", help="پاک کردن کل کش، نه فقط ویدیو")
    parser.add_argument("--yes", action="store_true", help="بدون سوال تاییدی اجرا کن")
    args = parser.parse_args()

    asyncio.run(main(clear_all=args.all, skip_confirm=args.yes))
