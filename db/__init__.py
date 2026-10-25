"""💾 دیتابیس

نکته: عمداً eager re-export نداره (همون الگوی utils/__init__.py) — چون
db.session در سطح ماژول engine می‌سازه و از bot.config نیاز به BOT_TOKEN
دارینه؛ eager ایمپورت یعنی حتی import کردن db.crud (که کاملاً مستقله) هم
BOT_TOKEN/دیتابیس می‌خواست و تست‌پذیری از بین می‌رفت.
همیشه مستقیم از زیرماژول ایمپورت کن:
    from db.models import Base
    from db.session import async_session_maker
    from db.crud import DownloadJobCRUD
"""
