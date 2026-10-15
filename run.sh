#!/usr/bin/env bash
# ./run.sh [start|setup|test|doctor|help]   (default: start web app + worker; Ctrl+C stops both)
set -euo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python
APP_PORT="${APP_PORT:-$(grep -E '^APP_PORT=' .env 2>/dev/null | cut -d= -f2 || true)}"
APP_PORT="${APP_PORT:-8000}"

need_venv() { [[ -x $PY ]] || { echo "Run ./run.sh setup first."; exit 1; }; }

case "${1:-start}" in
  setup)
    command -v python3 >/dev/null || { echo "python3 (3.12+) is required"; exit 1; }
    [[ -d .venv ]] || python3 -m venv .venv
    .venv/bin/pip install -q --upgrade pip
    .venv/bin/pip install -q -r requirements-worker.txt
    .venv/bin/playwright install chromium
    command -v ffmpeg >/dev/null || echo "!! FFmpeg not found: brew install ffmpeg (macOS) / apt install ffmpeg"
    if [[ ! -f .env ]]; then
      cp .env.example .env
      KEY=$($PY -c 'import secrets;print(secrets.token_urlsafe(48))')
      API=$($PY -c 'import secrets;print(secrets.token_urlsafe(32))')
      FERNET=$($PY -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')
      $PY - "$KEY" "$API" "$FERNET" <<'PYEOF'
import re, sys
key, api, fernet = sys.argv[1:]
s = open(".env").read()
s = re.sub(r"^SECRET_KEY=.*$", f"SECRET_KEY={key}", s, flags=re.M)
s = re.sub(r"^STUDIO_API_KEY=.*$", f"STUDIO_API_KEY={api}", s, flags=re.M)
s = re.sub(r"^WORKER_API_KEY=.*$", f"WORKER_API_KEY={api}", s, flags=re.M)
s = re.sub(r"^TOKEN_ENCRYPTION_KEY=.*$", f"TOKEN_ENCRYPTION_KEY={fernet}", s, flags=re.M)
open(".env", "w").write(s)
PYEOF
      chmod 600 .env
      echo "Created .env with fresh secrets. Add GOOGLE_CLIENT_ID/SECRET for Drive (see README)."
    fi
    mkdir -p var data && chmod 700 var data
    echo "Next: ./run.sh, then open the web app and create an account"
    ;;
  test)
    need_venv
    shift || true
    exec $PY -m pytest "$@"
    ;;
  doctor)
    need_venv
    echo "== Web app"; $PY -m app.doctor || true
    echo "== Worker";  $PY -m worker doctor
    ;;
  start)
    need_venv
    [[ -f .env ]] || { echo "No .env. Run ./run.sh setup first."; exit 1; }
    mkdir -p var/logs data && chmod 700 var data
    WORKER_PORT=$($PY -c 'from worker.config import get_settings;print(get_settings().API_PORT)')
    for port in "$WORKER_PORT" "$APP_PORT"; do
      if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "Port $port is already in use by:"; lsof -nP -iTCP:"$port" -sTCP:LISTEN | tail -n +2
        echo "Stop that process, or pick another port (API_PORT + WORKER_URL, or APP_PORT)."; exit 1
      fi
    done
    $PY -m worker serve &
    WORKER_PID=$!
    $PY -m uvicorn app.main:app --host 127.0.0.1 --port "$APP_PORT" --no-access-log &
    APP_PID=$!
    trap 'echo; echo "Stopping…"; kill $WORKER_PID $APP_PID 2>/dev/null; wait 2>/dev/null; exit 0' INT TERM
    echo "Web app: http://localhost:$APP_PORT   (worker on 127.0.0.1, Ctrl+C stops both)"
    # bash 3.2 (macOS) has no `wait -n`: poll until either process exits
    while kill -0 $WORKER_PID 2>/dev/null && kill -0 $APP_PID 2>/dev/null; do sleep 1; done
    echo "A process exited; stopping the other."
    kill $WORKER_PID $APP_PID 2>/dev/null || true
    wait 2>/dev/null || true
    ;;
  help|-h|--help)
    sed -n '2p' "$0"
    echo "  start   run web app (:$APP_PORT) and worker (127.0.0.1:8100)"
    echo "  setup   create .venv, install deps + Chromium, create .env with fresh secrets"
    echo "  test    run pytest (extra args passed through)"
    echo "  doctor  check configuration, FFmpeg, Playwright, Gemini session"
    ;;
  *) echo "Unknown command: $1 (try ./run.sh help)"; exit 1 ;;
esac
