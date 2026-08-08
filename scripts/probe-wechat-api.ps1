param(
    [string]$EnvPath = ".env",
    [string]$UploadImage = "",
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

$argsList = @("-m", "src.integrations.wechat_probe", "--env", $EnvPath)
if ($UploadImage) {
    $argsList += @("--upload-image", $UploadImage)
}
if ($Json) {
    $argsList += "--json"
}

& ".\.venv\Scripts\python.exe" @argsList
