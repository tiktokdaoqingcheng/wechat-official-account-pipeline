param(
    [string]$Seed = "",
    [string]$Topics = "config/topics.yaml",
    [string]$Schedule = "config/schedule.yaml",
    [string]$Safety = "config/safety-rules.yaml",
    [string]$NewsSources = "config/news-sources.yaml",
    [string]$NewsSeed = "",
    [string]$ManufacturingNewsSources = "config/manufacturing-news-sources.yaml",
    [string]$ManufacturingNewsSeed = "",
    [switch]$FetchNews,
    [string]$StateDir = "data/publish-state",
    [string]$EnvPath = ".env",
    [string]$OutputDir = "",
    [switch]$GenerateImages,
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
    "-m", "src.pipeline.daily_dry_run",
    "--topics", $Topics,
    "--schedule", $Schedule,
    "--safety", $Safety,
    "--news-sources", $NewsSources,
    "--manufacturing-news-sources", $ManufacturingNewsSources,
    "--state-dir", $StateDir,
    "--env", $EnvPath
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
if ($FetchNews) {
    $argsList += "--fetch-news"
}
if ($OutputDir) {
    $argsList += @("--output-dir", $OutputDir)
}
if ($GenerateImages) {
    $argsList += "--generate-images"
}
if ($Json) {
    $argsList += "--json"
}

& ".\.venv\Scripts\python.exe" @argsList
