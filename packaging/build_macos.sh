#!/bin/bash
# 构建 macOS .app 与 DMG。必须在 macOS 上运行。
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=$(python3 -c "import re;print(re.search(r'^VERSION = \"([\\d.]+)\"', open('zcode_resume_watcher.py', encoding='utf-8').read(), re.M).group(1))")
APP="dist/ZCode 续命监视器.app"
DMG="dist/ZCodeResumeWatcher-${VERSION}.dmg"

python3 -m PyInstaller --noconfirm --clean packaging/macos.spec

IDENTITY="${ZRW_SIGN_IDENTITY:--}"
if [ "$IDENTITY" = "-" ]; then
  codesign --deep --force --sign - "$APP" || true
else
  codesign --deep --force --options runtime --sign "$IDENTITY" "$APP"
fi

hdiutil create -volname "ZCode 续命监视器" -srcfolder "$APP" \
  -ov -format UDZO "$DMG"
echo "输出: $APP"
echo "DMG:  $DMG"
