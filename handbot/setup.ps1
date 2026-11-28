# The hand bot's setup on a new Windows computer, in one go, from the project folder (safe to run again):
#   powershell -ExecutionPolicy Bypass -File handbot\setup.ps1
# Installs Python's packages and Tesseract (with winget), writes the bot's settings to .env, then checks it all.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Get-Command tesseract -ErrorAction SilentlyContinue) -and
    -not (Test-Path "$env:ProgramFiles\Tesseract-OCR\tesseract.exe")) {
    Write-Host "Installing Tesseract (the bot reads the screen's words with it)..."
    winget install --id UB-Mannheim.TesseractOCR -e --accept-source-agreements --accept-package-agreements
}

Write-Host "Installing..."
if (-not (Test-Path .venv)) { py -3 -m venv .venv }
.\.venv\Scripts\python -m pip install -q -r requirements-bot.txt

if (-not (Test-Path .env)) { New-Item .env -ItemType File | Out-Null }
if (-not (Select-String -Path .env -Pattern '^BOT_API_KEY=.{24,}' -Quiet)) {
    $url = Read-Host "The app's address as this computer reaches it (Enter for the app on this computer)"
    $key = Read-Host "BOT_API_KEY (the same as the app's)"
    if ($url) { Add-Content .env "APP_URL=$url" }
    Add-Content .env "BOT_API_KEY=$key"
    Write-Host "Saved to .env"
}

Write-Host ""
Read-Host "Open Flow in Chrome (signed in), then press Enter to check the buttons on the screen"
.\.venv\Scripts\python -m handbot check
Write-Host ""
Write-Host "Start the bot with:  .venv\Scripts\python -m handbot run"
