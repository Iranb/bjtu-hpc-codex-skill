#!/bin/zsh
set -euo pipefail

LABEL="com.iranb.bjtu-hpc-menubar-monitor"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
RUNTIME_DIR="$HOME/Library/BJTUHPCMonitor"

launchctl bootout "gui/$UID" "$PLIST" >/dev/null 2>&1 || true
launchctl bootout "gui/$UID/$LABEL" >/dev/null 2>&1 || true
rm -f "$PLIST"
rm -rf "$RUNTIME_DIR"
echo "uninstalled $LABEL"
