param(
    [string]$OutputDirectory = "dist"
)

$ErrorActionPreference = "Stop"
$root = (& git rev-parse --show-toplevel).Trim()
if (-not $root) {
    throw "Run this script inside the project git repository."
}
Set-Location $root

& git diff --quiet
if ($LASTEXITCODE -ne 0) {
    throw "Tracked working-tree changes exist. Commit the stable release before packaging."
}
& git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    throw "Staged changes exist. Commit the stable release before packaging."
}

$commit = (& git rev-parse --short=12 HEAD).Trim()
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$destination = Join-Path $root $OutputDirectory
New-Item -ItemType Directory -Force -Path $destination | Out-Null
$archive = Join-Path $destination "wechat-auto-$timestamp-$commit.tar"
& git archive --format=tar --output=$archive HEAD
if ($LASTEXITCODE -ne 0) {
    throw "git archive failed."
}
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant()
[PSCustomObject]@{
    Commit = $commit
    Archive = $archive
    Sha256 = $hash
}
