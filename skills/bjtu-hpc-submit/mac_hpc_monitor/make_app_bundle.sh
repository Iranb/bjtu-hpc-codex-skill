#!/bin/zsh
set -euo pipefail

MONITOR_DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$MONITOR_DIR/BJTU HPC Monitor.app"
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
  <string>BJTU HPC Monitor</string>
  <key>CFBundleDisplayName</key>
  <string>BJTU HPC Monitor</string>
  <key>CFBundleIdentifier</key>
  <string>com.iranb.bjtu-hpc-menubar-monitor</string>
  <key>CFBundleVersion</key>
  <string>1.0</string>
  <key>CFBundleShortVersionString</key>
  <string>1.0</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleExecutable</key>
  <string>BJTU HPC Monitor</string>
  <key>LSUIElement</key>
  <true/>
</dict>
</plist>
PLIST

cat > "$MACOS/BJTU HPC Monitor" <<'SH'
#!/bin/zsh
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/../../.." && pwd)"
exec /bin/zsh "$APP_DIR/run_hpc_menubar_monitor.sh"
SH

chmod +x "$MACOS/BJTU HPC Monitor"
echo "$APP"
