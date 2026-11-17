#!/bin/sh
# همگام‌سازی رابط وب: webapp/static -> cloudzyro/public
# (به‌جز config.js که تو هر نسخه جدا نگه‌داری می‌شه)
# بعد از هر تغییر تو رابط وب، این اسکریپت رو اجرا کن:
#   sh cloudzyro/sync-static.sh
set -e
cd "$(dirname "$0")/.."
for f in webapp/static/*; do
  name=$(basename "$f")
  if [ "$name" != "config.js" ]; then
    cp "$f" "cloudzyro/public/$name"
    echo "کپی شد: $name"
  fi
done
echo "تمام شد. config.js دست‌نخورده موند."
