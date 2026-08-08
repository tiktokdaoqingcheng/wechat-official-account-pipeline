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
    [string]$LogDir = "logs",
    [switch]$Json
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$LogPath = Join-Path $LogDir "daily-run-$Timestamp.log"

try {
    "[$(Get-Date -Format o)] daily-run started" | Tee-Object -FilePath $LogPath

    $Python = ".\.venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $Python)) {
        throw "Python environment is missing. Scheduled runs do not install or modify dependencies."
    }

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

    & $Python @argsList 2>&1 |
        Tee-Object -FilePath $LogPath -Append

    $ExitCode = $LASTEXITCODE
    if ($null -eq $ExitCode) {
        $ExitCode = 0
    }
    "[$(Get-Date -Format o)] daily-run finished with exit code $ExitCode" |
        Tee-Object -FilePath $LogPath -Append
    exit $ExitCode
}
catch {
    "[$(Get-Date -Format o)] daily-run failed: $($_.Exception.Message)" |
        Tee-Object -FilePath $LogPath -Append
    exit 1
}
