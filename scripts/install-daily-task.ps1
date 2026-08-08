param(
    [string]$TaskName = "WeChatOfficialAccountDailyRun",
    [string]$Time = "09:00",
    [string]$Seed = "",
    [string]$Topics = "config/topics.yaml",
    [string]$Schedule = "config/schedule.yaml",
    [string]$Safety = "config/safety-rules.yaml",
    [string]$StateDir = "data/publish-state",
    [string]$OutputRoot = "outputs",
    [string]$EnvPath = ".env",
    [string]$DbPath = "data/app.sqlite",
    [string]$LogDir = "logs",
    [switch]$EnableLocalTask,
    [switch]$Remove,
    [switch]$Status
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$ScriptPath = Join-Path $PSScriptRoot "daily-run-logged.ps1"

function Quote-Arg([string]$Value) {
    return '"' + ($Value -replace '"', '\"') + '"'
}

if ($Remove) {
    schtasks.exe /Delete /TN $TaskName /F | Out-Host
    exit $LASTEXITCODE
}

if ($Status) {
    schtasks.exe /Query /TN $TaskName /V /FO LIST | Out-Host
    exit $LASTEXITCODE
}

$ArgumentParts = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", (Quote-Arg $ScriptPath)
)
if ($Seed) {
    $ArgumentParts += @("-Seed", (Quote-Arg $Seed))
}
if ($Topics -ne "config/topics.yaml") {
    $ArgumentParts += @("-Topics", (Quote-Arg $Topics))
}
if ($Schedule -ne "config/schedule.yaml") {
    $ArgumentParts += @("-Schedule", (Quote-Arg $Schedule))
}
if ($Safety -ne "config/safety-rules.yaml") {
    $ArgumentParts += @("-Safety", (Quote-Arg $Safety))
}
if ($StateDir -ne "data/publish-state") {
    $ArgumentParts += @("-StateDir", (Quote-Arg $StateDir))
}
if ($OutputRoot -ne "outputs") {
    $ArgumentParts += @("-OutputRoot", (Quote-Arg $OutputRoot))
}
if ($EnvPath -ne ".env") {
    $ArgumentParts += @("-EnvPath", (Quote-Arg $EnvPath))
}
if ($DbPath -ne "data/app.sqlite") {
    $ArgumentParts += @("-DbPath", (Quote-Arg $DbPath))
}
if ($LogDir -ne "logs") {
    $ArgumentParts += @("-LogDir", (Quote-Arg $LogDir))
}
$TaskCommand = "powershell.exe " + ($ArgumentParts -join " ")

Write-Host "Installing scheduled task: $TaskName"
Write-Host "Project: $ProjectRoot"
Write-Host "Time: $Time"
Write-Host "Command: $TaskCommand"

schtasks.exe /Create /TN $TaskName /SC DAILY /ST $Time /TR $TaskCommand /F | Out-Host
$CreateExitCode = $LASTEXITCODE
if ($CreateExitCode -ne 0) {
    exit $CreateExitCode
}
if (-not $EnableLocalTask) {
    Disable-ScheduledTask -TaskName $TaskName | Out-Null
    Write-Host "Task created in disabled state. Use -EnableLocalTask only for an explicit local dry-run task."
}
exit 0
