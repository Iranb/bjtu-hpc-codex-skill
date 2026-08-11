# Windows Widget

The canonical Windows implementation is `assets/windows-widget`. It follows the same redacted snapshot and visible-login contract as the deployed Apple widget generation locked in `apple_native_widget_component_lock.json`, without sharing or inspecting WidgetKit UI source.

## Source Selection Gate

Before editing, building, or installing the Windows widget, run:

```powershell
& $env:HPC_PYTHON scripts\resolve_windows_widget.py
```

Proceed only when it returns `status: ok`, and build only `selected_source_root`. The Windows component lock must match the Apple contract authority's short version and build version. A separately copied Windows project is not an installation source.

## Two Windows Hosts

- `BjtuHpc.Desktop` is the WPF floating widget and works on Windows 10 and 11.
- `BjtuHpc.WidgetProvider` is the packaged `IWidgetProvider` implementation for the Windows 11 Widgets Board. It uses Adaptive Cards and size-aware `$host.widgetSize` conditions.

Both hosts read only the local redacted snapshot. They never read the account store, portal tokens, cookies, browser profiles, or SSH credentials. Snapshot reload is a read-only operation. Token refresh is a separate explicit action sent to the loopback dashboard endpoint.

The WPF host uses a notification-area icon and does not create a taskbar button. Its close button, `Esc`, and `Alt+F4` hide the window while leaving snapshot monitoring active. Double-click the notification icon or choose **Show widget** to restore it. The icon menu also provides **Reload snapshot**, **Open dashboard**, and the only deliberate **Exit** action. Windows decides whether the icon is shown directly beside the clock or inside the notification-area overflow (the hidden-icons chevron).

## Background Data Contract

The desktop installer must install the complete data path, not only the WPF executable:

- Persist the current Python 3.12 executable as user `HPC_PYTHON` and the skill `scripts` directory as user `SLURM_DIR`.
- Install and start the per-user dashboard/Token Guardian task.
- Run one synchronous redacted snapshot query and refuse to finish when it contains no accounts.
- Register and start the per-user `BJTU HPC Widget Snapshot` task. It runs `hpc_native_widget_snapshot.py` continuously and writes `%LOCALAPPDATA%\BJTUHPCWidget\snapshot.json` atomically.

The task may query live read-only queue state. It never submits, cancels, uploads, downloads, or reads credentials into the snapshot. The WPF process watches the resulting file and reloads automatically.

## Version 3.5 / Build 19 Contract

- Resolve GPU and CPU availability from a valid reported free count; fall back to `total - allocated` for missing, impossible, or contradictory legacy values.
- Preserve per-account visible login with `{ "account": "alias" }`.
- Provide an explicit all-account visible refresh with `{ "accounts": "all" }`.
- Keep account aliases local and redacted; never add tokens or portal response bodies to widget data.
- Show compact resource status at small size, account rows at medium and large sizes, and node/legend detail at large size.
- Use the bundled WPF resource dictionary for a compact Soft UI Evolution design: restrained depth, strong contrast, a consistent type scale, text plus color for status, visible keyboard focus, named automation controls, and PerMonitorV2 DPI support.
- Keep `ShowInTaskbar="False"`, retain `ShutdownMode="OnExplicitShutdown"`, and dispose the notification icon on explicit application exit so no stale tray icon remains.

## Build and Install

Use the installed .NET SDK explicitly if it is not on `PATH`:

```powershell
$Dotnet = Join-Path $env:ProgramFiles 'dotnet\dotnet.exe'
& $Dotnet test assets\windows-widget\tests\BjtuHpc.Widget.Core.Tests\BjtuHpc.Widget.Core.Tests.csproj
& $Dotnet build assets\windows-widget\src\BjtuHpc.Desktop\BjtuHpc.Desktop.csproj -c Release
& $Dotnet build assets\windows-widget\src\BjtuHpc.WidgetProvider\BjtuHpc.WidgetProvider.csproj -c Release -r win-x64 -p:Platform=x64 -p:AppxPackageSigningEnabled=false
& assets\windows-widget\scripts\install-desktop.ps1 -EnableStartup -PythonPath $env:HPC_PYTHON
```

Verify the live path without exposing account aliases:

```powershell
Get-ScheduledTask -TaskName 'BJTU HPC Widget Snapshot'
Test-Path (Join-Path $env:LOCALAPPDATA 'BJTUHPCWidget\snapshot.json')
& $env:HPC_PYTHON scripts\hpc_dashboard_service.py status
```

Do not install the MSIX provider on Windows 10 because the Windows 11 Widgets Board host is unavailable. Building the provider remains a useful compatibility check.
