#!/bin/zsh
set -euo pipefail

LABEL="com.iranb.bjtu-hpc-desktop-widget"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
SLURM_DIR="$(cd "$SOURCE_DIR/.." && pwd)"
RUNTIME_DIR="$HOME/Library/BJTUHPCWidget"
RUNNER="$RUNTIME_DIR/run_hpc_desktop_widget.sh"

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$RUNTIME_DIR"
cp "$SOURCE_DIR/hpc_desktop_widget.py" "$RUNTIME_DIR/hpc_desktop_widget.py"
cp "$SOURCE_DIR/hpc_menubar_monitor.py" "$RUNTIME_DIR/hpc_menubar_monitor.py"
cp "$SOURCE_DIR/run_hpc_desktop_widget.sh" "$RUNTIME_DIR/run_hpc_desktop_widget.sh"
chmod +x "$RUNTIME_DIR/hpc_desktop_widget.py" "$RUNTIME_DIR/run_hpc_desktop_widget.sh"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>$RUNNER</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>StandardOutPath</key>
  <string>/tmp/bjtu_hpc_desktop_widget.out.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/bjtu_hpc_desktop_widget.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HPC_MONITOR_INTERVAL</key>
    <string>${HPC_MONITOR_INTERVAL:-60}</string>
    <key>HPC_MONITOR_MAX_INTERVAL</key>
    <string>${HPC_MONITOR_MAX_INTERVAL:-600}</string>
    <key>HPC_MONITOR_TIMEOUT</key>
    <string>${HPC_MONITOR_TIMEOUT:-45}</string>
    <key>HPC_MONITOR_ACCOUNT_CAP</key>
    <string>${HPC_MONITOR_ACCOUNT_CAP:-4}</string>
    <key>HPC_MONITOR_PYTHON</key>
    <string>${HPC_MONITOR_PYTHON:-python3}</string>
    <key>HPC_MONITOR_SLURM_DIR</key>
    <string>$SLURM_DIR</string>
    <key>HPC_WIDGET_WIDTH</key>
    <string>${HPC_WIDGET_WIDTH:-320}</string>
    <key>HPC_WIDGET_HEIGHT</key>
    <string>${HPC_WIDGET_HEIGHT:-370}</string>
    <key>HPC_WIDGET_ALWAYS_ON_TOP</key>
    <string>${HPC_WIDGET_ALWAYS_ON_TOP:-1}</string>
    <key>HPC_WIDGET_ALL_SPACES</key>
    <string>${HPC_WIDGET_ALL_SPACES:-0}</string>
  </dict>
</dict>
</plist>
PLIST

chmod 600 "$PLIST"
chmod +x "$RUNNER"

launchctl bootout "gui/$UID" "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$UID" "$PLIST"
launchctl kickstart -k "gui/$UID/$LABEL"
echo "installed $LABEL"
echo "plist: $PLIST"
echo "runtime: $RUNTIME_DIR"
echo "runner: $RUNNER"
pgrep -afil "hpc_desktop_widget.py" || true
