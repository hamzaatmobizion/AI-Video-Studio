# .\run.ps1 [start|setup|test|doctor|help]   (Windows; default: start web app + worker; Ctrl+C stops both)
# The same as ./run.sh on a Mac or Linux. If PowerShell won't run scripts, start it with:
#   powershell -ExecutionPolicy Bypass -File run.ps1 [command]
param([string]$Command = "start")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$PY = ".venv\Scripts\python.exe"

function Get-EnvValue([string]$name) {
    if (-not (Test-Path .env)) { return $null }
    $line = Select-String -Path .env -Pattern "^$name=(.*)$" | Select-Object -First 1
    if ($line) { return $line.Matches[0].Groups[1].Value.Trim() }
    return $null
}

$APP_PORT = $env:APP_PORT
if (-not $APP_PORT) { $APP_PORT = Get-EnvValue "APP_PORT" }
if (-not $APP_PORT) { $APP_PORT = "8000" }

function Need-Venv {
    if (-not (Test-Path $PY)) { Write-Host "Run .\run.ps1 setup first."; exit 1 }
}

function Test-PortBusy([int]$port) {
    $busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($busy) {
        Write-Host "Port $port is already in use by:"
        $busy | ForEach-Object { Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue } |
            Format-Table Id, ProcessName -AutoSize | Out-String | Write-Host
        Write-Host "Stop that process, or pick another port (API_PORT + WORKER_URL, or APP_PORT)."
        return $true
    }
    return $false
}

switch ($Command) {
    "setup" {
        if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
            Write-Host "Python 3 (3.12+) is required: install it from python.org (tick ""Add to PATH"")."; exit 1
        }
        if (-not (Test-Path .venv)) { py -3 -m venv .venv }
        & $PY -m pip install -q --upgrade pip
        & $PY -m pip install -q -r requirements-worker.txt -r requirements-bot.txt
        & .venv\Scripts\playwright.exe install chromium
        if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
            Write-Host "Installing FFmpeg (the app joins the clips with it)..."
            winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
            Write-Host "!! Open a new PowerShell window so it finds ffmpeg, then start the app there."
        }
        if (-not (Test-Path .env)) {
            Copy-Item .env.example .env
            $code = @'
import re, secrets
from cryptography.fernet import Fernet
key, api, fernet = secrets.token_urlsafe(48), secrets.token_urlsafe(32), Fernet.generate_key().decode()
s = open(".env").read()
s = re.sub(r"^SECRET_KEY=.*$", f"SECRET_KEY={key}", s, flags=re.M)
s = re.sub(r"^STUDIO_API_KEY=.*$", f"STUDIO_API_KEY={api}", s, flags=re.M)
s = re.sub(r"^WORKER_API_KEY=.*$", f"WORKER_API_KEY={api}", s, flags=re.M)
s = re.sub(r"^TOKEN_ENCRYPTION_KEY=.*$", f"TOKEN_ENCRYPTION_KEY={fernet}", s, flags=re.M)
open(".env", "w").write(s)
'@
            $code | & $PY -
            Write-Host "Created .env with fresh secrets. Add GOOGLE_CLIENT_ID/SECRET for Drive (see README)."
        }
        New-Item -ItemType Directory -Force var, data | Out-Null
        Write-Host "Next: the hand bot's settings with  powershell -ExecutionPolicy Bypass -File handbot\setup.ps1"
        Write-Host "then .\run.ps1, open the web app and create an account."
    }
    "test" {
        Need-Venv
        & $PY -m pytest @args
        exit $LASTEXITCODE
    }
    "doctor" {
        Need-Venv
        Write-Host "== Web app"; & $PY -m app.doctor
        Write-Host "== Worker";  & $PY -m worker doctor
    }
    "start" {
        Need-Venv
        if (-not (Test-Path .env)) { Write-Host "No .env. Run .\run.ps1 setup first."; exit 1 }
        New-Item -ItemType Directory -Force var\logs, data | Out-Null
        $WORKER_PORT = (& $PY -c "from worker.config import get_settings;print(get_settings().API_PORT)").Trim()
        foreach ($port in @($WORKER_PORT, $APP_PORT)) { if (Test-PortBusy ([int]$port)) { exit 1 } }
        $worker = Start-Process -FilePath $PY -ArgumentList "-m", "worker", "serve" -NoNewWindow -PassThru
        $app = Start-Process -FilePath $PY -NoNewWindow -PassThru -ArgumentList "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", $APP_PORT, "--no-access-log"
        Write-Host "Web app: http://localhost:$APP_PORT   (worker on 127.0.0.1, Ctrl+C stops both)"
        try {
            while (-not $worker.HasExited -and -not $app.HasExited) { Start-Sleep -Seconds 1 }
            Write-Host "A process exited; stopping the other."
        } finally {
            # Ctrl+C reaches both (they share this window) and they shut down by themselves, the app stopping a hand
            # bot it started. Whatever is still running after 10 seconds is stopped with everything it started
            # (taskkill /T), so a hand bot never keeps moving the mouse after the app has gone.
            $end = (Get-Date).AddSeconds(10)
            while ((-not $app.HasExited -or -not $worker.HasExited) -and (Get-Date) -lt $end) { Start-Sleep -Milliseconds 500 }
            foreach ($p in @($app, $worker)) {
                if (-not $p.HasExited) { taskkill /PID $p.Id /T /F | Out-Null }
            }
        }
    }
    { $_ -in "help", "-h", "--help" } {
        Write-Host ".\run.ps1 [start|setup|test|doctor|help]"
        Write-Host "  start   run web app (:$APP_PORT) and worker (127.0.0.1:8100)"
        Write-Host "  setup   create .venv, install deps (app, worker, hand bot) + Chromium + FFmpeg, create .env"
        Write-Host "  test    run pytest (extra args passed through)"
        Write-Host "  doctor  check configuration, FFmpeg, Playwright, Gemini session"
    }
    default { Write-Host "Unknown command: $Command (try .\run.ps1 help)"; exit 1 }
}
