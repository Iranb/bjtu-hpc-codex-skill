# BJTU HPC macOS Monitor

Lightweight macOS monitors for saved BJTU HPC accounts.

Both monitors poll the existing helper command:

```bash
hpc_queue_summary.py --json
```

The menu bar title shows total running and pending jobs, for example:

```text
HPC R6/10 Q5
```

The menu is organized as:

```text
DASHBOARD
RUN 6/10   JOBS 11/20   WAIT 5

ACCOUNTS
main    FULL    R2/2  J4/4  Q2
other   FULL    R2/2  J4/4  Q2
account_c   ROOM    R2/2  J3/4  Q1
account_d     IDLE    R0/2  J0/4  Q0
```

Each account opens a submenu with token time, pending reason, and compact job rows. It does not display portal tokens, passwords, cookies, or temporary SSH certificates.

## Desktop Widget

The graphical widget is a compact floating, draggable desktop panel with account cards, status colors, and running GPU/CPU totals.

Account rows use this compact shape:

```text
main   Run 2   G2 C8    Jobs 4/4   Wait 2
account_c  Run 2   G4 C32   Jobs 3/4   Wait 1
```

Preview:

```bash
./hpc_desktop_widget.py --preview /tmp/bjtu_hpc_widget_preview.png
```

Launch manually:

```bash
./make_widget_app_bundle.sh
open "BJTU HPC Widget.app"
```

Install at login:

```bash
./install_hpc_desktop_widget.sh
```

Uninstall:

```bash
./uninstall_hpc_desktop_widget.sh
```

Right-click the widget to open the dashboard or quit. Drag anywhere on the widget to move it.
Click the pin icon in the top-right corner to toggle Always On Top; the choice is saved in
`~/Library/Application Support/BJTUHPCWidget/config.json`.
By default, Always On Top is scoped to the current macOS Space. Set
`HPC_WIDGET_ALL_SPACES=1` before launch or install if the widget should follow
every Space.
The bottom-right legend maps account-name colors: green `full`, blue `room`,
amber `open`, purple `token` for accounts that need token refresh, and red
`err` for non-auth query or network errors.

## Run Once

```bash
./hpc_menubar_monitor.py --once
```

## Launch Manually

```bash
./make_app_bundle.sh
open "BJTU HPC Monitor.app"
```

## Install At Login

```bash
./install_hpc_menubar_monitor.sh
```

Uninstall:

```bash
./uninstall_hpc_menubar_monitor.sh
```

Logs for the LaunchAgent:

```text
/tmp/bjtu_hpc_menubar_monitor.out.log
/tmp/bjtu_hpc_menubar_monitor.err.log
/tmp/bjtu_hpc_desktop_widget.out.log
/tmp/bjtu_hpc_desktop_widget.err.log
```

## Configuration

Set environment variables before launching or installing:

```bash
export HPC_MONITOR_SLURM_DIR=/path/to/bjtu-hpc-helper
export HPC_MONITOR_PYTHON=python3
export HPC_MONITOR_INTERVAL=60
export HPC_MONITOR_MAX_INTERVAL=600
export HPC_MONITOR_TIMEOUT=45
export HPC_MONITOR_ACCOUNTS=main,other
export HPC_MONITOR_ALL_PARTITIONS=0
export HPC_WIDGET_WIDTH=320
export HPC_WIDGET_HEIGHT=466
export HPC_WIDGET_ALWAYS_ON_TOP=1
export HPC_WIDGET_ALL_SPACES=0
```

Defaults are suitable when the monitor lives inside the helper workspace.

`HPC_MONITOR_INTERVAL` is the base refresh interval. The monitors compare a
stable state signature after each refresh; if jobs and cluster GPU/CPU resources
are unchanged, the next interval grows linearly up to
`HPC_MONITOR_MAX_INTERVAL`. Any job-state change, pending-reason change, or
cluster node GPU/CPU allocation/free-count change resets the interval to the
base value.

The desktop widget also shows GPU-node resource usage from native Slurm
`scontrol show node`; active reservation nodes are excluded from the displayed
GPU/CPU totals and node list.
