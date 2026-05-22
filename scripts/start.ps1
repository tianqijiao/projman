$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)

if (Test-Path ".env") {
    Get-Content ".env" | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) {
            return
        }
        $parts = $line.Split("=", 2)
        if ($parts.Count -ne 2) {
            return
        }
        $key = $parts[0].Trim()
        $value = $parts[1].Trim().Trim('"').Trim("'")
        if ($key -and -not [Environment]::GetEnvironmentVariable($key, "Process")) {
            [Environment]::SetEnvironmentVariable($key, $value, "Process")
        }
    }
}

if (-not (Test-Path ".venv")) {
    uv venv .venv
}

if (-not $env:PROJMAN_USERNAME) {
    $env:PROJMAN_USERNAME = "admin"
}
if (-not $env:PROJMAN_PASSWORD) {
    $env:PROJMAN_PASSWORD = "admin"
}
if (-not $env:PROJMAN_SECRET_KEY) {
    $env:PROJMAN_SECRET_KEY = "projman-local-secret"
}

Write-Host "运维项目管理工具启动中..."
Write-Host "本机访问:     http://127.0.0.1:8765"
Write-Host "局域网/Tailscale: http://<这台电脑的IP>:8765"
Write-Host "默认账号: $env:PROJMAN_USERNAME"
Write-Host "默认密码: $env:PROJMAN_PASSWORD"

uv run uvicorn app.main:app --host 0.0.0.0 --port 8765
