param(
    [string]$PythonPath,
    [string]$TaskName = 'BJTU HPC Widget Snapshot'
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$SkillRoot = Split-Path -Parent (Split-Path -Parent $ProjectRoot)
$ControllerScripts = Join-Path $SkillRoot 'scripts'
$SnapshotWriter = Join-Path $ControllerScripts 'hpc_native_widget_snapshot.py'
$DashboardService = Join-Path $ControllerScripts 'hpc_dashboard_service.py'
$SnapshotPath = Join-Path $env:LOCALAPPDATA 'BJTUHPCWidget\snapshot.json'

if (-not $PythonPath) {
    $PythonPath = $env:HPC_PYTHON
}
if (-not $PythonPath) {
    $PythonPath = [Environment]::GetEnvironmentVariable('HPC_PYTHON', 'User')
}
if (-not $PythonPath) {
    $PythonPath = (Get-Command python -ErrorAction Stop).Source
}

$PythonPath = [System.IO.Path]::GetFullPath($PythonPath)
$ControllerScripts = [System.IO.Path]::GetFullPath($ControllerScripts)
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "HPC Python was not found: $PythonPath"
}
if (-not (Test-Path -LiteralPath $SnapshotWriter -PathType Leaf)) {
    throw "Snapshot writer was not found: $SnapshotWriter"
}

[Environment]::SetEnvironmentVariable('HPC_PYTHON', $PythonPath, 'User')
[Environment]::SetEnvironmentVariable('SLURM_DIR', $ControllerScripts, 'User')
$env:HPC_PYTHON = $PythonPath
$env:SLURM_DIR = $ControllerScripts

& $PythonPath $DashboardService install
if ($LASTEXITCODE -ne 0) {
    throw "Dashboard service installation failed with exit code $LASTEXITCODE"
}

& $PythonPath $SnapshotWriter --once --no-reload `
    --python $PythonPath --slurm-dir $ControllerScripts --snapshot-path $SnapshotPath
if ($LASTEXITCODE -ne 0) {
    throw "Initial widget snapshot failed with exit code $LASTEXITCODE"
}

$snapshot = Get-Content -LiteralPath $SnapshotPath -Raw | ConvertFrom-Json
$accountCount = @($snapshot.payload.accounts).Count
if ($accountCount -lt 1) {
    throw 'Initial widget snapshot contains no accounts; the background task was not registered.'
}

function Quote-TaskArgument([string]$Value) {
    return '"' + $Value.Replace('"', '\"') + '"'
}

$arguments = @(
    (Quote-TaskArgument $SnapshotWriter),
    '--python', (Quote-TaskArgument $PythonPath),
    '--slurm-dir', (Quote-TaskArgument $ControllerScripts),
    '--snapshot-path', (Quote-TaskArgument $SnapshotPath),
    '--no-reload'
) -join ' '

$userId = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $PythonPath -Argument $arguments -WorkingDirectory $ControllerScripts
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description 'Writes the redacted local snapshot consumed by the BJTU HPC Windows widget.'

Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Output "Installed $TaskName for $accountCount redacted account(s)."
Write-Output "Snapshot: $SnapshotPath"
