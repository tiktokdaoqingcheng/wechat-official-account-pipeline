param(
    [string]$EnvPath = ".env"
)

$ErrorActionPreference = "Stop"

function Read-SecretPlainText {
    param([string]$Prompt)

    $secure = Read-Host -Prompt $Prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
}

$values = [ordered]@{}
$values["WECHAT_APP_ID"] = Read-Host "WECHAT_APP_ID"
$values["WECHAT_APP_SECRET"] = Read-SecretPlainText "WECHAT_APP_SECRET"
$values["MODEL_PROVIDER"] = Read-Host "MODEL_PROVIDER"
$values["MODEL_BASE_URL"] = Read-Host "MODEL_BASE_URL"
$values["MODEL_NAME"] = Read-Host "MODEL_NAME"
$values["MODEL_API_KEY"] = Read-SecretPlainText "MODEL_API_KEY"
$values["APP_ENV"] = "development"
$values["LOG_LEVEL"] = "info"
$values["DATABASE_URL"] = "sqlite:///data/app.sqlite"

$lines = foreach ($item in $values.GetEnumerator()) {
    "$($item.Key)=$($item.Value)"
}

Set-Content -LiteralPath $EnvPath -Value $lines -Encoding UTF8
Write-Host "Wrote $EnvPath"
