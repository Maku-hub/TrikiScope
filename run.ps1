# Launcher for TrikiScope - no venv activation needed.
# Usage:
#   .\run.ps1                      # auto-connect
#   .\run.ps1 --scan               # pass through any CLI args
#   .\run.ps1 --mode complementary
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "[!] Virtual environment not found. Create it first:" -ForegroundColor Yellow
    Write-Host "    python -m venv .venv"
    Write-Host "    .venv\Scripts\python.exe -m pip install -r requirements.txt"
    exit 1
}

if ($args.Count -eq 0) {
    & $python -m trikiscope --auto-connect
} else {
    & $python -m trikiscope @args
}
