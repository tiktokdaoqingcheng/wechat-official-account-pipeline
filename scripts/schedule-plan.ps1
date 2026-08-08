param(
    [string]$Schedule = "config/schedule.yaml",
    [string]$Article = "",
    [string]$Review = "",
    [string]$Decision = "",
    [string]$Output = "",
    [switch]$Json
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"

if (-not $Article) {
    throw "-Article is required."
}
if (-not $Review) {
    throw "-Review is required."
}
if (-not $Decision) {
    throw "-Decision is required."
}

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}

& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt | Out-Host

$argsList = @(
    "-m", "src.scheduler.schedule_plan",
    "--schedule", $Schedule,
    "--article", $Article,
    "--review", $Review,
    "--decision", $Decision
)
if ($Output) {
    $argsList += @("--output", $Output)
}
if ($Json) {
    $argsList += "--json"
}

& ".\.venv\Scripts\python.exe" @argsList
