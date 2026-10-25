"""
🔧 ابزارها

نکته: قبلاً این‌جا همه‌چیز از زیرماژول‌ها eager re-export می‌شد
(LinkDetector, MetadataExtractor, ...). حذفش کردیم چون:
۱. هیچ‌جای پروژه از `from utils import X` استفاده نمی‌کرد - همه‌جا مستقیم
   از زیرماژول import می‌شه (مثلاً `from utils.link_detector import ...`)،
   پس این re-export کاملاً بلااستفاده بود.
۲. باعث می‌شد import کردن یه ماژول کاملاً مستقل و بدون وابستگی مثل
   link_detector.py (که فقط از stdlib استفاده می‌کنه) هم مجبور بشه
   metadata.py رو لود کنه و در نتیجه به loguru/mutagen وابسته بشه -
   یعنی نمی‌شد link_detector رو جدا و سبک تست/ایمپورت کرد.
اگه لازم شد، هر ماژول رو مستقیم از مسیر خودش import کن:
    from utils.link_detector import LinkDetector, SiteType
    from utils.metadata import MetadataExtractor
"""
