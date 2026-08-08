param(
    [string]$EnvPath = ".env",
    [string]$Schedule = "config/schedule.yaml",
    [string]$OutputDir = "",
    [string]$StateDir = "data/publish-state",
    [int]$RetryCount = -1,
    [double]$RetryDelaySeconds = -1,
    [switch]$Json
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}

& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt | Out-Host

$argsList = @(
    "-m", "src.pipeline.create_test_draft",
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
