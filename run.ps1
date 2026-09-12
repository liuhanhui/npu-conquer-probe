#Requires -Version 5.1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path .\.venv\Scripts\python.exe)) {
  Write-Host "Creating venv..."
  python -m venv .venv
}

& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
Write-Host ""
Write-Host "Open http://127.0.0.1:8787"
Write-Host "Ctrl+C to stop"
& .\.venv\Scripts\python.exe -m app.main
