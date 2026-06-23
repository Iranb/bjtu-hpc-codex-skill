#!/bin/zsh
set -euo pipefail

LABEL="local.bjtu-hpc-desktop-widget"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$UID" "$PLIST" >/dev/null 2>&1 || true
rm -f "$PLIST"
echo "uninstalled $LABEL"
