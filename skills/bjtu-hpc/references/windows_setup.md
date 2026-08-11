# Windows Controller Setup

Use native 64-bit Python 3.12 in PowerShell. WSL, Bash, and a local Slurm installation are not required for portal status, SSH-proxy access, data transfer, or verified native submission: Slurm syntax and `sbatch --test-only` checks run on the remote cluster through the authenticated proxy.

## Configure the Controller

From the skill's `scripts` directory:

```powershell
$HpcPython = "C:\Path\To\Python312\python.exe"
$Scripts = (Resolve-Path ".").Path

[Environment]::SetEnvironmentVariable("HPC_PYTHON", $HpcPython, "User")
[Environment]::SetEnvironmentVariable("SLURM_DIR", $Scripts, "User")

& $HpcPython -m pip install -r (Join-Path $Scripts "requirements.txt")
& $HpcPython -m playwright install chromium
& $HpcPython (Join-Path $Scripts "hpc_doctor.py") --json
```

Open a new terminal after changing user environment variables. For the current PowerShell session, also assign `$env:HPC_PYTHON` and `$env:SLURM_DIR` explicitly.

## Private Files and Migration

The controller restricts secret-bearing files to the current Windows user and `SYSTEM`. It rejects inherited or explicit access for broad principals such as `Everyone`, `Users`, or `Authenticated Users`. Do not commit account stores, credentials, tokens, migration exports, browser profiles, private ledgers, intents, or receipts.

Use `hpc_accounts.py export-json` and `import-json` for an encrypted migration. Keep the export outside Git, use a strong passphrase, validate all imported accounts, and delete the transport copy only after the new controller is verified and the user authorizes cleanup.

## Read-only Verification

Start with diagnostics and state reads. Do not submit, cancel, upload, download, delete, or change remote permissions unless the user explicitly authorizes that mutation.

```powershell
& $env:HPC_PYTHON "$env:SLURM_DIR\hpc_doctor.py" --json
& $env:HPC_PYTHON "$env:SLURM_DIR\hpc_accounts.py" validate --all
& $env:HPC_PYTHON "$env:SLURM_DIR\hpc_queue_summary.py" --json
```

Keep raw JSON local when it contains account aliases, usernames, host details, job identifiers, or paths. Report only the fields needed by the user.

## Dashboard Background Task

On Windows, `hpc_dashboard_service.py` installs a per-user Task Scheduler job that starts at sign-in. The task launches the same redacted local dashboard and Token Guardian used by the foreground helper. It does not grant submission authority.

```powershell
& $env:HPC_PYTHON "$env:SLURM_DIR\hpc_dashboard_service.py" install
& $env:HPC_PYTHON "$env:SLURM_DIR\hpc_dashboard_service.py" status
& $env:HPC_PYTHON "$env:SLURM_DIR\hpc_dashboard_service.py" restart
& $env:HPC_PYTHON "$env:SLURM_DIR\hpc_dashboard_service.py" uninstall
```

The default endpoint is `http://127.0.0.1:8765`. The task runs only for the signed-in user and uses the configured Python executable and script directory. On macOS the same helper continues to manage a LaunchAgent.

## Windows Widget

Read `windows_widget.md` before editing or installing the Windows widget. Run `scripts\resolve_windows_widget.py` and use only its selected canonical source under `assets\windows-widget`; do not reinstall from an older standalone copy. On Windows 10, install the WPF desktop host. The packaged provider may be built for compatibility, but requires the Windows 11 Widgets Board to run.

## Troubleshooting

- If an account file is rejected, inspect its NTFS ACL and remove inherited or broad-principal access; do not weaken the check.
- If the dashboard task fails, run the underlying `hpc_transfer_web.py` command in a foreground PowerShell window, then inspect Task Scheduler history.
- If authentication returns `11009`, `11011`, `11012`, or HTTP `401`, use the visible refresh flow for only the affected saved account.
- If remote submission preflight reports a Bash or Slurm error, fix the exact candidate script. Installing local WSL does not replace the remote checks.
