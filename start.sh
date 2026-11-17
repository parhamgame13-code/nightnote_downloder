#!/bin/sh
# نقطه‌ی شروع همه‌کاره: وب + بات + ورکر + beat (از طریق run_all.py).
# روی Render/Railway/Fly.io و سرور، همین فایل (یا `python run_all.py`) اجرا می‌شه.
set -e
cd "$(dirname "$0")"
exec python run_all.py
