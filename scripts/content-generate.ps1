param(
    [string]$Seed = "",
    [string]$Output = "",
    [switch]$Json
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"

if (-not $Seed) {
    throw "-Seed is required."
}

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}

& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt | Out-Host

$argsList = @("-m", "src.pipeline.content_engine", "--seed", $Seed)
if ($Output) {
    $argsList += @("--output", $Output)
}
if ($Json) {
    $argsList += "--json"
}

& ".\.venv\Scripts\python.exe" @argsList
