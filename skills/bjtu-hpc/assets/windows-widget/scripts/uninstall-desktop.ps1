$ErrorActionPreference = 'Stop'
$InstallRoot = Join-Path $env:LOCALAPPDATA 'Programs\BJTUHPCWidget'
$ExpectedParent = [System.IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'Programs'))
$ResolvedInstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)
if ((Split-Path -Leaf $ResolvedInstallRoot) -ne 'BJTUHPCWidget' -or
    (Split-Path -Parent $ResolvedInstallRoot) -ne $ExpectedParent) {
    throw "Refusing to uninstall unexpected path: $ResolvedInstallRoot"
}
$InstalledExe = Join-Path $ResolvedInstallRoot 'BjtuHpc.Desktop.exe'
$SnapshotTaskName = 'BJTU HPC Widget Snapshot'
$Shortcuts = @(
    (Join-Path ([Environment]::GetFolderPath('Desktop')) 'BJTU HPC Widget.lnk'),
    (Join-Path ([Environment]::GetFolderPath('Startup')) 'BJTU HPC Widget.lnk')
)

Get-Process -Name 'BjtuHpc.Desktop' -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and ([System.IO.Path]::GetFullPath($_.Path) -eq $InstalledExe) } |
    Stop-Process
foreach ($Shortcut in $Shortcuts) {
    if (Test-Path -LiteralPath $Shortcut) { Remove-Item -LiteralPath $Shortcut }
}
if (Test-Path -LiteralPath $ResolvedInstallRoot) {
    Remove-Item -LiteralPath $ResolvedInstallRoot -Recurse
}
if (Get-ScheduledTask -TaskName $SnapshotTaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $SnapshotTaskName -Confirm:$false
}
Write-Output 'BJTU HPC desktop widget uninstalled. Redacted snapshots and window preferences were preserved.'
