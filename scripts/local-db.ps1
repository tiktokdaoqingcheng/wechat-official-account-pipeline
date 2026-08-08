param(
    [Parameter(Position = 0)]
    [string]$Command = "recent-runs",
    [string]$DbPath = "data/app.sqlite",
    [int]$Limit = 10,
    [int]$Days = 7,
    [string]$OutputDir = "outputs/run-summary",
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

$argsList = @("-m", "src.storage.local_db_cli", "--db", $DbPath)
if ($Json) {
    $argsList += "--json"
}
$argsList += $Command

if ($Command -eq "recent-runs") {
    $argsList += @("--limit", $Limit)
}
elseif ($Command -eq "summary") {
    $argsList += @("--days", $Days, "--limit", $Limit, "--output-dir", $OutputDir)
}

& ".\.venv\Scripts\python.exe" @argsList
