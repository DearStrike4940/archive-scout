#!/bin/bash
set -euo pipefail

APP_NAME="Archive Scout"
ASSET_NAME="ArchiveScout-macOS-Intel.dmg"
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

rm -rf build dist release dmg-root
mkdir -p release dmg-root

python -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --onedir \
  --name "$APP_NAME" \
  --osx-bundle-identifier "org.archivescout.desktop" \
  --target-arch x86_64 \
  run_app.py

APP_PATH="dist/${APP_NAME}.app"
PLIST_PATH="$APP_PATH/Contents/Info.plist"

/usr/libexec/PlistBuddy -c "Add :LSMinimumSystemVersion string 12.0" "$PLIST_PATH" 2>/dev/null || \
/usr/libexec/PlistBuddy -c "Set :LSMinimumSystemVersion 12.0" "$PLIST_PATH"
/usr/libexec/PlistBuddy -c "Add :NSHighResolutionCapable bool true" "$PLIST_PATH" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleDisplayName string '$APP_NAME'" "$PLIST_PATH" 2>/dev/null || true

codesign --force --deep --sign - "$APP_PATH"
codesign --verify --deep --strict --verbose=2 "$APP_PATH"

cp -R "$APP_PATH" dmg-root/
ln -s /Applications dmg-root/Applications

hdiutil create \
  -volname "$APP_NAME" \
  -srcfolder dmg-root \
  -ov \
  -format UDZO \
  "release/$ASSET_NAME"

cd release
shasum -a 256 "$ASSET_NAME" > "$ASSET_NAME.sha256"
