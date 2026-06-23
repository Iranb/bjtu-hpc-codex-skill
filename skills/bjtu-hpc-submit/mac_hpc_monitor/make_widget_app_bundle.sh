#!/bin/zsh
set -euo pipefail

MONITOR_DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$MONITOR_DIR/BJTU HPC Widget.app"
MACOS="$APP/Contents/MacOS"
RESOURCES="$APP/Contents/Resources"

rm -rf "$APP"
mkdir -p "$MACOS" "$RESOURCES"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>
  <string>BJTU HPC Widget</string>
  <key>CFBundleDisplayName</key>
  <string>BJTU HPC Widget</string>
  <key>CFBundleIdentifier</key>
  <string>local.bjtu-hpc-desktop-widget</string>
  <key>CFBundleVersion</key>
  <string>1.0</string>
  <key>CFBundleShortVersionString</key>
  <string>1.0</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleExecutable</key>
  <string>BJTU HPC Widget</string>
  <key>LSUIElement</key>
  <true/>
</dict>
</plist>
PLIST

cat > "$MACOS/BJTU HPC Widget" <<'SH'
#!/bin/zsh
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/../../.." && pwd)"
exec /bin/zsh "$APP_DIR/run_hpc_desktop_widget.sh"
SH

chmod +x "$MACOS/BJTU HPC Widget"
echo "$APP"
