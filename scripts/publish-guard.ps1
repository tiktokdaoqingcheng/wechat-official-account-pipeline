param(
    [Parameter(Position = 0)]
    [string]$Command = "status",
    [string]$StateDir = "data/publish-state",
    [string]$OutputDir = "",
    [string]$Title = "",
    [string]$Action = "publish",
    [string]$ConfirmationFile = "",
    [string]$MediaId = "",
    [string]$PublishId = "",
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

$argsList = @("-m", "src.review.publish_guard_cli", "--state-dir", $StateDir)
if ($Json) {
    $argsList += "--json"
}
$argsList += $Command

if ($Command -eq "request-confirmation") {
    if (-not $OutputDir) {
        throw "-OutputDir is required for request-confirmation."
    }
    if (-not $Title) {
        throw "-Title is required for request-confirmation."
    }
    $argsList += @("--output-dir", $OutputDir, "--action", $Action, "--title", $Title)
}
elseif ($Command -eq "check-confirmation") {
    if (-not $ConfirmationFile) {
        throw "-ConfirmationFile is required for check-confirmation."
    }
    $argsList += @("--confirmation-file", $ConfirmationFile)
}
elseif ($Command -eq "record-lock") {
    if (-not $Title) {
        throw "-Title is required for record-lock."
    }
    $argsList += @("--title", $Title)
    if ($MediaId) {
        $argsList += @("--media-id", $MediaId)
    }
    if ($PublishId) {
        $argsList += @("--publish-id", $PublishId)
    }
}

& ".\.venv\Scripts\python.exe" @argsList
