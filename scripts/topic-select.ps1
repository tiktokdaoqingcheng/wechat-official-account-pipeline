param(
    [string]$Topics = "config/topics.yaml",
    [string]$Output = "",
    [string]$Date = "",
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

$argsList = @("-m", "src.pipeline.topic_engine", "--topics", $Topics)
if ($Output) {
    $argsList += @("--output", $Output)
}
if ($Date) {
    $argsList += @("--date", $Date)
}
if ($Json) {
    $argsList += "--json"
}

& ".\.venv\Scripts\python.exe" @argsList
