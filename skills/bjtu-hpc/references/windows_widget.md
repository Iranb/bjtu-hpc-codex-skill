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

## Version 3.5 / Build 19 Contract

- Resolve GPU and CPU availability from a valid reported free count; fall back to `total - allocated` for missing, impossible, or contradictory legacy values.
- Preserve per-account visible login with `{ "account": "alias" }`.
- Provide an explicit all-account visible refresh with `{ "accounts": "all" }`.
- Keep account aliases local and redacted; never add tokens or portal response bodies to widget data.
- Show compact resource status at small size, account rows at medium and large sizes, and node/legend detail at large size.

## Build and Install

Use the installed .NET SDK explicitly if it is not on `PATH`:

```powershell
$Dotnet = Join-Path $env:ProgramFiles 'dotnet\dotnet.exe'
& $Dotnet test assets\windows-widget\tests\BjtuHpc.Widget.Core.Tests\BjtuHpc.Widget.Core.Tests.csproj
& $Dotnet build assets\windows-widget\src\BjtuHpc.Desktop\BjtuHpc.Desktop.csproj -c Release
& $Dotnet build assets\windows-widget\src\BjtuHpc.WidgetProvider\BjtuHpc.WidgetProvider.csproj -c Release -r win-x64 -p:Platform=x64 -p:AppxPackageSigningEnabled=false
& assets\windows-widget\scripts\install-desktop.ps1 -EnableStartup
```

Do not install the MSIX provider on Windows 10 because the Windows 11 Widgets Board host is unavailable. Building the provider remains a useful compatibility check.
