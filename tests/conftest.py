import sys
from pathlib import Path

# ریشه‌ی پروژه رو به sys.path اضافه می‌کنیم تا import هایی مثل
# `from utils.link_detector import ...` بدون نصب پکیج هم کار کنن.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
