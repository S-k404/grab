#!/bin/zsh
# Build Grab.app (native macOS window around the grab GUI).
#   ./build_app.sh                   -> ./Grab.app
#   ./build_app.sh --install         -> also copies it to ~/Applications (shows in Launchpad/Spotlight)
#   ./build_app.sh --update-runtime  -> only refresh the app's engine (after editing grab/ or updating yt-dlp)
#
# The app runs its own copy of the engine in ~/Library/Application Support/Grab/runtime, so it never
# needs permission to read this (Documents) folder.
set -e
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"
PROJECT="$PWD"
APP="$PROJECT/Grab.app"
BUILD="$PROJECT/app/build"
RUNTIME="$HOME/Library/Application Support/Grab/runtime"
mkdir -p "$BUILD"

[ -x .venv/bin/grab-gui ] || ./setup.sh

echo "Updating app engine in $RUNTIME…"
PY="$(.venv/bin/python -c 'import sys; print(sys._base_executable)')"
[ -x "$RUNTIME/bin/python" ] || uv venv -q --python "$PY" "$RUNTIME"
uv pip install -q --python "$RUNTIME/bin/python" -U --reinstall-package grab "$PROJECT" "yt-dlp[default,curl-cffi]" gallery-dl
# a copy of the browser extension outside Documents, so browsers can load it without asking for folder access
EXT_COPY="$HOME/Library/Application Support/Grab/browser-extension"
mkdir -p "$EXT_COPY" && rsync -a --delete --exclude package.json browser-extension/ "$EXT_COPY/"
[ "$1" = "--update-runtime" ] && { echo "Engine updated."; exit 0; }

echo "Compiling…"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
swiftc -O -swift-version 5 app/main.swift -o "$APP/Contents/MacOS/Grab" -framework AppKit -framework WebKit

echo "Drawing icon…"
swiftc -O app/make_icon.swift -o "$BUILD/make_icon" -framework AppKit
"$BUILD/make_icon" "$BUILD/icon.png"
ICONSET="$BUILD/Grab.iconset"
rm -rf "$ICONSET" && mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s "$BUILD/icon.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$BUILD/icon.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/Grab.icns"

cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Grab</string>
  <key>CFBundleDisplayName</key><string>Grab</string>
  <key>CFBundleIdentifier</key><string>local.grab.downloader</string>
  <key>CFBundleExecutable</key><string>Grab</string>
  <key>CFBundleIconFile</key><string>Grab</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>15.4</string>
  <key>CFBundleURLTypes</key>
  <array><dict>
    <key>CFBundleURLName</key><string>Send to Grab</string>
    <key>CFBundleURLSchemes</key><array><string>grab</string></array>
  </dict></array>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSHumanReadableCopyright</key><string>yt-dlp · gallery-dl · aria2c · ffmpeg</string>
  <key>GrabProjectDir</key><string>$PROJECT</string>
  <key>GrabRuntime</key><string>$RUNTIME</string>
</dict>
</plist>
EOF

codesign --force --deep -s - "$APP" >/dev/null 2>&1 || true
touch "$APP"
echo "Built $APP"

if [ "$1" = "--install" ]; then
  mkdir -p "$HOME/Applications"
  rm -rf "$HOME/Applications/Grab.app"
  cp -R "$APP" "$HOME/Applications/"
  # make the installed copy the one that opens grab:// links
  LSR=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
  "$LSR" -u "$APP" >/dev/null 2>&1 || true
  "$LSR" -f "$HOME/Applications/Grab.app" >/dev/null 2>&1 || true
  echo "Installed to ~/Applications/Grab.app"
fi
