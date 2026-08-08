param(
    [string]$Seed = "",
    [string]$Topics = "config/topics.yaml",
    [string]$Schedule = "config/schedule.yaml",
    [string]$Safety = "config/safety-rules.yaml",
    [string]$NewsSources = "config/news-sources.yaml",
    [string]$NewsSeed = "",
    [string]$ManufacturingNewsSources = "config/manufacturing-news-sources.yaml",
    [string]$ManufacturingNewsSeed = "",
    [switch]$SkipFetchNews,
    [string]$StateDir = "data/publish-state",
    [string]$OutputRoot = "outputs",
    [string]$EnvPath = ".env",
    [string]$DbPath = "data/app.sqlite",
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
    "-m", "src.scheduler.daily_runner",
    "--topics", $Topics,
    "--schedule", $Schedule,
    "--safety", $Safety,
    "--news-sources", $NewsSources,
    "--manufacturing-news-sources", $ManufacturingNewsSources,
    "--state-dir", $StateDir,
    "--output-root", $OutputRoot,
    "--env", $EnvPath,
    "--db", $DbPath
)
if ($Seed) {
    $argsList += @("--seed", $Seed)
}
if ($NewsSeed) {
    $argsList += @("--news-seed", $NewsSeed)
}
if ($ManufacturingNewsSeed) {
    $argsList += @("--manufacturing-news-seed", $ManufacturingNewsSeed)
}
if ($SkipFetchNews) {
    $argsList += "--skip-fetch-news"
}
if ($Json) {
    $argsList += "--json"
}

& ".\.venv\Scripts\python.exe" @argsList
