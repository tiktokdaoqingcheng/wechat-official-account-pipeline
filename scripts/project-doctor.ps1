param([switch]$Strict)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$issues = [System.Collections.Generic.List[string]]::new()
$warnings = [System.Collections.Generic.List[string]]::new()

Write-Host "Project doctor is read-only: it never fetches, pulls, pushes, deploys, or calls external APIs."
$root = (& git rev-parse --show-toplevel 2>$null | Select-Object -First 1)
if (-not $root) {
    Write-Host "[FAIL] Current directory is not inside a Git repository."
    exit 2
}
$root = $root.Trim()
Set-Location $root
Write-Host "[INFO] Git root: $root"
$branch = (& git branch --show-current | Select-Object -First 1).Trim()
Write-Host "[INFO] Branch: $branch"
& git show-ref --verify --quiet "refs/heads/$branch"
if ($LASTEXITCODE -eq 0) {
    $head = (& git log -1 --oneline --decorate | Select-Object -First 1)
    Write-Host "[INFO] Latest commit: $head"
} else {
    $warnings.Add("Repository has no commit yet.")
}

$remotes = @(& git remote -v)
if ($remotes.Count -eq 0) { Write-Host "[INFO] No remote configured." } else { $remotes | ForEach-Object { Write-Host "[INFO] $_" } }

$status = @(& git status --short --branch --ignored)
$status | ForEach-Object { Write-Host $_ }

$required = @(
    "README.md", "README.zh-CN.md", "LICENSE", "CONTRIBUTING.md", "SECURITY.md",
    "CHANGELOG.md", "THIRD_PARTY_NOTICES.md", ".env.example", ".github/workflows/ci.yml"
)
foreach ($path in $required) {
    if (-not (Test-Path -LiteralPath $path)) { $issues.Add("Missing required open-source file: $path") }
}

$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) {
    & python scripts/check_public_tree.py
    if ($LASTEXITCODE -ne 0) { $issues.Add("Public tree safety check failed.") }
} else {
    $warnings.Add("Python was not found; public tree safety check was skipped.")
}

foreach ($warning in $warnings) { Write-Host "[WARN] $warning" -ForegroundColor Yellow }
foreach ($issue in $issues) { Write-Host "[FAIL] $issue" -ForegroundColor Red }
if ($issues.Count -gt 0) { exit 2 }
if ($Strict -and $warnings.Count -gt 0) { exit 1 }
Write-Host "[OK] Project doctor found no blocking issue." -ForegroundColor Green
