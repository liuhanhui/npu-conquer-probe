#Requires -Version 5.1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
  Write-Error "未找到 node，请先安装 Node.js 18+"
}

if (-not (Test-Path .\node_modules)) {
  Write-Host "Installing server dependencies..."
  npm install
}

if (-not (Test-Path .\web\dist\index.html)) {
  Write-Host "Building Vue frontend..."
  npm run build:web
}

if (-not (Test-Path .\.env)) {
  Copy-Item .\.env.example .\.env
  Write-Host "已创建 .env，请填写 NPU_HOSTS 后重新运行"
}

Write-Host ""
Write-Host "Open http://127.0.0.1:8787"
Write-Host "Ctrl+C to stop"
npm start
