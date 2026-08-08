param(
    [Parameter(Mandatory=$true)]
    [string]$Package,
    [string]$EnvPath = ".env",
    [string]$Schedule = "config/schedule.yaml",
    [string]$OutputDir = "",
    [string]$StateDir = "data/publish-state",
    [int]$RetryCount = -1,
    [double]$RetryDelaySeconds = -1,
    [switch]$Json
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}

& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt | Out-Host

$argsList = @(
    "-m", "src.pipeline.create_draft_from_package",
    "--package", $Package,
    "--env", $EnvPath,
    "--schedule", $Schedule,
    "--state-dir", $StateDir
)
if ($OutputDir) {
    $argsList += @("--output-dir", $OutputDir)
}
if ($RetryCount -ge 0) {
    $argsList += @("--retry-count", $RetryCount)
}
if ($RetryDelaySeconds -ge 0) {
    $argsList += @("--retry-delay-seconds", $RetryDelaySeconds)
}
if ($Json) {
    $argsList += "--json"
}

& ".\.venv\Scripts\python.exe" @argsList
