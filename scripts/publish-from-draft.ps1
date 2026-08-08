param(
    [string]$EnvPath = ".env",
    [string]$Schedule = "config/schedule.yaml",
    [string]$Review = "",
    [string]$MediaId = "",
    [string]$Title = "",
    [string]$ConfirmationFile = "",
    [string]$StateDir = "data/publish-state",
    [string]$OutputDir = "",
    [switch]$Poll,
    [int]$PollAttempts = 6,
    [double]$PollIntervalSeconds = 10,
    [int]$RetryCount = -1,
    [double]$RetryDelaySeconds = -1,
    [switch]$Json
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"

if (-not $Review) {
    throw "-Review is required."
}
if (-not $MediaId) {
    throw "-MediaId is required."
}

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}

& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt | Out-Host

$argsList = @(
    "-m", "src.pipeline.publish_from_draft",
    "--env", $EnvPath,
    "--schedule", $Schedule,
    "--review", $Review,
    "--media-id", $MediaId,
    "--state-dir", $StateDir,
    "--poll-attempts", $PollAttempts,
    "--poll-interval-seconds", $PollIntervalSeconds
)
if ($Title) {
    $argsList += @("--title", $Title)
}
if ($ConfirmationFile) {
    $argsList += @("--confirmation-file", $ConfirmationFile)
}
if ($OutputDir) {
    $argsList += @("--output-dir", $OutputDir)
}
if ($Poll) {
    $argsList += "--poll"
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
