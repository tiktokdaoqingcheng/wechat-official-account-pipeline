param(
    [Parameter(Mandatory=$true)]
    [string]$ArticleHtml,
    [string]$Output = ""
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$source = Resolve-Path $ArticleHtml
$target = if ($Output) {
    $Output
} else {
    Join-Path (Split-Path $source -Parent) "preview.html"
}
$targetPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($target)

$body = Get-Content -Raw -Encoding UTF8 $source
$articleDir = Split-Path $source -Parent
$articleJson = Join-Path $articleDir "article.json"
$illustration = Join-Path $articleDir "illustration.png"
if ((Test-Path $articleJson) -and (Test-Path $illustration)) {
    $article = Get-Content -Raw -Encoding UTF8 $articleJson | ConvertFrom-Json
    $placeholder = [string]$article.illustration_placeholder
    if ($placeholder) {
        $targetDir = Split-Path $targetPath -Parent
        if ((Resolve-Path $targetDir).Path -eq (Resolve-Path $articleDir).Path) {
            $illustrationUri = "illustration.png"
        } else {
            $illustrationUri = "file:///" + ((Resolve-Path $illustration).Path -replace '\\', '/')
        }
        $body = $body.Replace($placeholder, $illustrationUri)
    }
}
$page = @"
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>WeChat HTML Preview</title>
  <style>
    body {
      margin: 0;
      background: #eef2f7;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    main {
      width: min(430px, calc(100vw - 24px));
      margin: 24px auto;
      padding: 20px 16px;
      background: #fff;
      box-shadow: 0 14px 40px rgba(15, 23, 42, 0.12);
    }
  </style>
</head>
<body>
  <main>
$body
  </main>
</body>
</html>
"@

Set-Content -Encoding UTF8 -Path $targetPath -Value $page
Write-Output $targetPath
