#!/bin/zsh
set -euo pipefail

WIDGET_DIR="$(cd "$(dirname "$0")" && pwd)"
SLURM_DIR="${HPC_MONITOR_SLURM_DIR:-$(cd "$WIDGET_DIR/.." && pwd)}"

export HPC_MONITOR_PYTHON="${HPC_MONITOR_PYTHON:-python3}"
export HPC_MONITOR_SLURM_DIR="${HPC_MONITOR_SLURM_DIR:-$SLURM_DIR}"
export HPC_MONITOR_INTERVAL="${HPC_MONITOR_INTERVAL:-60}"
export HPC_MONITOR_MAX_INTERVAL="${HPC_MONITOR_MAX_INTERVAL:-600}"
export HPC_MONITOR_TIMEOUT="${HPC_MONITOR_TIMEOUT:-45}"
export HPC_MONITOR_DASHBOARD_URL="${HPC_MONITOR_DASHBOARD_URL:-http://127.0.0.1:8765/}"
export HPC_WIDGET_WIDTH="${HPC_WIDGET_WIDTH:-320}"
export HPC_WIDGET_HEIGHT="${HPC_WIDGET_HEIGHT:-466}"

# The widget reads saved BJTU account files through helper scripts. It should
# not inherit unrelated API keys or ad-hoc portal tokens from the launching shell.
unset HPC_PARA_ATOKEN
unset PAPERNEXUS_REMOTE_API_TOKEN
unset OPENAI_API_KEY
unset ANTHROPIC_API_KEY
unset GEMINI_API_KEY

exec "$HPC_MONITOR_PYTHON" "$WIDGET_DIR/hpc_desktop_widget.py"
