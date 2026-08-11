param(
    [switch]$EnableStartup,
    [switch]$DemoSnapshot,
    [switch]$NoLaunch,
    [switch]$NoBackground,
    [string]$PythonPath
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$InstallRoot = Join-Path $env:LOCALAPPDATA 'Programs\BJTUHPCWidget'
$PublishRoot = Join-Path $ProjectRoot 'artifacts\desktop-win-x64'
$Dotnet = Join-Path $env:ProgramFiles 'dotnet\dotnet.exe'
$InstalledExe = Join-Path $InstallRoot 'BjtuHpc.Desktop.exe'

if (-not (Test-Path -LiteralPath $Dotnet)) {
    throw ".NET SDK was not found at $Dotnet"
}

& $Dotnet publish (Join-Path $ProjectRoot 'src\BjtuHpc.Desktop\BjtuHpc.Desktop.csproj') `
    -c Release -r win-x64 --self-contained true `
    -p:DebugType=None -o $PublishRoot

$running = Get-Process -Name 'BjtuHpc.Desktop' -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and ([System.IO.Path]::GetFullPath($_.Path) -eq [System.IO.Path]::GetFullPath($InstalledExe)) }
if ($running) {
    $running | Stop-Process -Force
    $running | Wait-Process -Timeout 10 -ErrorAction SilentlyContinue
}

New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
Copy-Item -Path (Join-Path $PublishRoot '*') -Destination $InstallRoot -Recurse -Force

$Shell = New-Object -ComObject WScript.Shell
$DesktopShortcut = $Shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'BJTU HPC Widget.lnk'))
$DesktopShortcut.TargetPath = $InstalledExe
$DesktopShortcut.WorkingDirectory = $InstallRoot
$DesktopShortcut.Save()

if ($EnableStartup) {
    $StartupShortcut = $Shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Startup')) 'BJTU HPC Widget.lnk'))
    $StartupShortcut.TargetPath = $InstalledExe
    $StartupShortcut.WorkingDirectory = $InstallRoot
    $StartupShortcut.Save()
}

if ($DemoSnapshot) {
    $effectivePython = if ($PythonPath) { $PythonPath } elseif ($env:HPC_PYTHON) { $env:HPC_PYTHON } else { (Get-Command python -ErrorAction Stop).Source }
    & $effectivePython (Join-Path $ProjectRoot 'tools\snapshot_bridge.py') --demo
} elseif (-not $NoBackground) {
    & (Join-Path $PSScriptRoot 'install-background.ps1') -PythonPath $PythonPath
}

if (-not $NoLaunch) {
    Start-Process -FilePath $InstalledExe -WorkingDirectory $InstallRoot
}

Write-Output "Installed BJTU HPC Windows widget 3.5 / build 19 to $InstallRoot"
