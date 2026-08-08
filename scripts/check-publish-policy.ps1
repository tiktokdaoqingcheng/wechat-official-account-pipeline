param(
    [string]$Review = "data/sample-review-low-risk.json",
    [string]$Schedule = "config/schedule.yaml",
    [switch]$AlreadyPublishedToday
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}

& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt | Out-Host

$argsList = @("-m", "src.review.publish_policy_cli", "--schedule", $Schedule, "--review", $Review)
if ($AlreadyPublishedToday) {
    $argsList += "--already-published-today"
}

& ".\.venv\Scripts\python.exe" @argsList
