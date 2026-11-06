#!/bin/sh
# Builds "Jarvis for Mac.app" and the DMG from this folder.
#
#   scripts/build.sh                 → build/Jarvis for Mac.app and build/Jarvis-for-Mac-<version>.dmg
#   scripts/build.sh --install       → also copies it into /Applications
#
# Needs Xcode's command line tools (for swiftc): xcode-select --install
# The app is signed "ad hoc" (on this Mac), not by an Apple developer account; see README, "Gatekeeper".
set -eu

VERSION="1.0.0"
REPO_URL="${REPO_URL:-}"
UV_VERSION="0.13.1"
UV_SHA256="cfe454573412ff7380d15fc7dae5997ab53ffce72fce0a78e91ddbc84001acd2"

SRC="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$SRC/build"
# Compile from a neutral folder, so no path from the builder's Mac (like a user name) ends up inside the binaries.
WORK="$(mktemp -d /private/tmp/jarvis-build.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/src"
cp -R "$SRC/app" "$SRC/helpers" "$SRC/overlay" "$WORK/src/"
cd "$WORK/src"

SWIFTC="swiftc -O -swift-version 5 -file-prefix-map $WORK/src=."
APP="$WORK/Jarvis for Mac.app"
RES="$APP/Contents/Resources"
mkdir -p "$APP/Contents/MacOS" "$RES/jarvis/helpers" "$RES/jarvis/overlay" "$RES/bin"

echo "→ Building the helpers"
for h in JarvisAudio JarvisDictate JarvisMusic JarvisScreen; do
  $SWIFTC -o "$RES/jarvis/helpers/$h" "helpers/$h.swift"
done
for h in JarvisPIM JarvisMessenger; do   # these carry their own Info.plist, which macOS needs for their permissions
  $SWIFTC -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "helpers/$h-Info.plist" \
    -o "$RES/jarvis/helpers/$h" "helpers/$h.swift"
done
$SWIFTC -o "$RES/jarvis/overlay/JarvisOverlay" overlay/JarvisOverlay.swift

echo "→ Building the app"
$SWIFTC -o "$APP/Contents/MacOS/Jarvis" app/JarvisApp.swift

echo "→ Copying Jarvis's code"
( cd "$SRC" && cp -R jarvis.py jarvis_core setup assets requirements.lock LICENSE "$RES/jarvis/" )
find "$RES/jarvis" -name "__pycache__" -type d -prune -exec rm -rf {} +
find "$RES/jarvis" -name ".DS_Store" -delete

echo "→ Adding uv $UV_VERSION (installs Jarvis's Python), checked against its published fingerprint"
curl -fsSL --proto '=https' -o "$WORK/uv.tar.gz" \
  "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-aarch64-apple-darwin.tar.gz"
[ "$(shasum -a 256 "$WORK/uv.tar.gz" | awk '{print $1}')" = "$UV_SHA256" ] || { echo "uv didn't match its fingerprint"; exit 1; }
tar -xzf "$WORK/uv.tar.gz" -C "$WORK"
cp "$WORK/uv-aarch64-apple-darwin/uv" "$RES/bin/uv"

echo "→ Icon"
$SWIFTC -o "$WORK/make_icon" app/make_icon.swift
"$WORK/make_icon" "$WORK/icon.png" >/dev/null
mkdir -p "$WORK/Jarvis.iconset"
for s in 16 32 128 256 512; do
  sips -z $s $s "$WORK/icon.png" --out "$WORK/Jarvis.iconset/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$WORK/icon.png" --out "$WORK/Jarvis.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$WORK/Jarvis.iconset" -o "$RES/Jarvis.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>com.jarvisformac.app</string>
  <key>CFBundleName</key><string>Jarvis for Mac</string>
  <key>CFBundleDisplayName</key><string>Jarvis</string>
  <key>CFBundleExecutable</key><string>Jarvis</string>
  <key>CFBundleIconFile</key><string>Jarvis</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSArchitecturePriority</key><array><string>arm64</string></array>
  <key>LSUIElement</key><true/>
  <key>NSHumanReadableCopyright</key><string>Free and open source (MIT).</string>
  <key>JarvisRepoURL</key><string>$REPO_URL</string>
  <key>NSMicrophoneUsageDescription</key><string>Jarvis listens for "Hey Jarvis", to what you ask, and to what you dictate with the fn key. Audio is processed on this Mac and never saved or sent anywhere.</string>
  <key>NSContactsUsageDescription</key><string>Jarvis looks up the number of the person you name when you ask him to message them.</string>
  <key>NSCalendarsFullAccessUsageDescription</key><string>Jarvis reads your calendar when you ask what's on, and adds events when you ask.</string>
  <key>NSRemindersFullAccessUsageDescription</key><string>Jarvis adds and reads reminders when you ask.</string>
  <key>NSAppleEventsUsageDescription</key><string>Jarvis controls apps like Music, WhatsApp and Messages when you ask him to. He never sends a message without your spoken yes.</string>
</dict>
</plist>
PLIST

echo "→ Signing (ad hoc)"
xattr -cr "$APP"
for b in "$RES"/jarvis/helpers/* "$RES/jarvis/overlay/JarvisOverlay" "$RES/bin/uv"; do
  codesign --force --sign - --timestamp=none "$b"
done
codesign --force --sign - --timestamp=none --identifier com.jarvisformac.app "$APP"
codesign --verify --deep --strict "$APP"

echo "→ Checking nothing from this Mac is inside"
# Text files: no path from this Mac. (The repository link in Info.plist is meant to be there.)
if grep -rIl -e "$HOME" -e "/Users/" -e "/private/tmp/jarvis-build" "$APP" 2>/dev/null; then echo "Found a local path in the app (above)."; exit 1; fi
# Compiled programs: no path and no user name from this Mac.
for b in "$APP/Contents/MacOS/Jarvis" "$RES"/jarvis/helpers/* "$RES/jarvis/overlay/JarvisOverlay"; do
  if strings "$b" | grep -q -e "$HOME" -e "/Users/" -e "jarvis-build" -e "$(whoami)"; then echo "Something from this Mac is inside $b"; exit 1; fi
done

echo "→ Making the DMG"
rm -rf "$OUT/Jarvis for Mac.app"; mkdir -p "$OUT"
ditto "$APP" "$OUT/Jarvis for Mac.app"
STAGE="$WORK/dmg"; mkdir -p "$STAGE"
ditto "$APP" "$STAGE/Jarvis for Mac.app"
ln -s /Applications "$STAGE/Applications"
DMG="$OUT/Jarvis-for-Mac-$VERSION.dmg"
rm -f "$DMG"
hdiutil create -quiet -volname "Jarvis for Mac" -srcfolder "$STAGE" -fs HFS+ -format UDZO -ov "$DMG"
( cd "$OUT" && shasum -a 256 "$(basename "$DMG")" > "$(basename "$DMG").sha256" )
echo "✓ $OUT/Jarvis for Mac.app"
echo "✓ $DMG ($(du -h "$DMG" | awk '{print $1}'))"
cat "$DMG.sha256"

if [ "${1:-}" = "--install" ]; then
  rm -rf "/Applications/Jarvis for Mac.app" && ditto "$OUT/Jarvis for Mac.app" "/Applications/Jarvis for Mac.app" \
    && echo "✓ Installed: /Applications/Jarvis for Mac.app"
fi
