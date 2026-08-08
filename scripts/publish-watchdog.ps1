param(
    [string]$Date = "",
    [string]$OutputRoot = "outputs",
    [string]$StateDir = "data/publish-state",
    [string]$EnvPath = ".env",
    [string]$ExpectedBy = "09:30",
    [switch]$Notify,
    [switch]$Json
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot
$Python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python environment is missing. Run project setup before the watchdog."
}

$argsList = @(
    "-m", "src.monitoring.publish_watchdog",
    "--output-root", $OutputRoot,
    "--state-dir", $StateDir,
    "--env", $EnvPath,
    "--expected-by", $ExpectedBy
)
if ($Date) { $argsList += @("--date", $Date) }
if ($Notify) { $argsList += "--notify" }
if ($Json) { $argsList += "--json" }

& $Python @argsList
exit $LASTEXITCODE
