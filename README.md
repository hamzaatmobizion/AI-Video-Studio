# VideoGeneration

Write one prompt, pick a length (10 seconds to 2 minutes), and get **one video** made by the Gemini web app
(Veo), saved to your Google Drive, playable in the browser. Gemini's chat first splits your prompt into
10-second scenes (one per 10 seconds; no video quota used), then each scene is made as one clip and the clips
are joined with ffmpeg (hard cuts). "Write scenes myself" lets you write the scenes instead. Each scene uses one Gemini video from the daily limit. If
the limit is hit partway, the video pauses with its finished scenes kept and continues after
**Resume generation** in Settings. Editing, transitions and Meta publishing come in later phases.

```
Web app (FastAPI :8000) ──polls──► Worker (FastAPI 127.0.0.1:8100, Bearer key)
  login · studio · settings              one job = one clip
  Drive OAuth · upload · Range stream    Playwright → gemini.google.com → MP4
```

## Setup (macOS)
```bash
brew install ffmpeg            # if missing
./run.sh setup                 # .venv, deps, Playwright Chromium, .env with fresh secrets
./run.sh                       # web app + worker; Ctrl+C stops both
```
Then open http://localhost:8000 (or your `APP_PORT`), click **Create one** to sign up, and:
1. **Settings → Open Gemini sign-in window.** A normal browser window opens on the worker's own
   profile (`data/browser-profile`). Sign in to Google, open Gemini once, click **Done signing in**.
   (CLI alternative: `.venv/bin/python -m worker login`.)
2. `.venv/bin/python -m worker check-gemini` shows which UI selectors match and saves a screenshot to
   `data/diagnostics/`. If something is ✗ (Gemini changed its page), edit `worker/gemini_ui.json`.
   No code changes are needed.
3. **Settings → Connect Google Drive** (needs the OAuth client below).
4. **Studio:** write a prompt, choose 9:16 or 16:9, then click Generate.

Other commands: `./run.sh test`, `./run.sh doctor`, `./run.sh help`.
To reset a forgotten password: `.venv/bin/python -m app.create_user` (same email, new password).

**Accounts:** anyone who can reach the app can sign up. Each account has its own videos and its own
Google Drive connection; the Gemini worker (and its Settings controls) is shared by everyone.
Videos made before accounts existed belong to the first account.

## Environment (`.env`)
| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | (none; required) | Session signing, at least 32 characters |
| `DATABASE_URL` | `sqlite:///./var/app.db` | SQLAlchemy URL (PostgreSQL works) |
| `COOKIE_SECURE` | `false` | `true` behind HTTPS |
| `WORKER_URL` / `WORKER_API_KEY` | `http://127.0.0.1:8100` / (none) | Worker address and key (= `STUDIO_API_KEY`) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | (none) | Google OAuth web client |
| `GOOGLE_DRIVE_FOLDER_NAME` | `VideoGeneration` | Default Drive folder |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Used for the OAuth redirect URI |
| `TOKEN_ENCRYPTION_KEY` | (none) | Fernet key for the stored Drive refresh token |
| `VAR_DIR` | `./var` | App DB and temp downloads |
| `SCHEDULER_ENABLED` | `true` | Background polling and uploads |
| `STUDIO_DATA_PATH` | `./data` | Worker DB, clips, profile, diagnostics |
| `BROWSER_PROFILE_PATH` | `data/browser-profile` | Dedicated Gemini browser profile |
| `API_HOST` / `API_PORT` | `127.0.0.1` / `8100` | Worker bind; only loopback is accepted |
| `STUDIO_API_KEY` | (none) | Worker Bearer key, at least 24 characters |
| `GEMINI_MODEL_LABEL` | (unset; see `profile.json`) | Model to pick in Gemini's picker; empty = keep the current one |
| `GENERATION_TIMEOUT_SECONDS` | `900` | Maximum wait for Gemini to finish one clip |
| `MAX_RETRIES` | `1` | Extra attempts after a QC or Gemini error |
| `BROWSER_HEADLESS` | `true` | Generate in the background with no browser window; `false` to watch or debug |
| `BROWSER_CHANNEL` | (empty) | `chrome` = use installed Google Chrome |

## Google Drive OAuth setup
1. In Google Cloud Console, create a project and enable the **Google Drive API**.
2. Set up the OAuth consent screen: External, add yourself as a test user, and add the scope `.../auth/drive.file`.
3. Under Credentials, create an **OAuth client ID** of type **Web application**, with this authorised redirect URI:
   `http://localhost:8000/auth/google/callback` (in general, `PUBLIC_BASE_URL` + `/auth/google/callback`).
4. Put the client ID and secret in `.env` and restart.

Scope is `drive.file`: the app sees only files and folders it created. Only the refresh token is stored,
Fernet-encrypted. Disconnect revokes it at Google.

Saved as `<folder>/<YYYY-MM>/<date> <prompt> (<id>).mp4`. Each upload is verified (exists, not trashed,
same size and MD5) before the temp copy is deleted. If an upload fails, it retries every 30 s, up to 10 times.
When Drive is full, the card shows **Try saving again**, which re-uploads the same file without generating a new one.
The player streams through the app with Range support, so the file is never made public.
Cancel on a finished video moves it to the Drive trash. Dismiss only hides the card.

## Worker contract (the app polls; the worker never calls back)
```
POST   /videos          {"prompt","aspect","ref"} JSON, or multipart with up to 3 images → {"job_id"}
GET    /videos/{id}     → {"status": queued|running|done|failed, "error", "video_url", "final_prompt"}
GET    /videos/{id}/file → MP4 (Bearer)
DELETE /videos/{id}     → best-effort cancel
GET    /health          → {"account_paused", "queued", "running", "gemini", ...}
GET    /gemini/status · POST /gemini/login · /gemini/login/done · /gemini/check · /gemini/resume
```
- `error` is human-readable and shown verbatim.
- A 4xx is a final failure. A 5xx or network error is retried on the next poll, and the job fails after 10 in a row.
- A job that hasn't finished after 30 minutes is cancelled.

## Account safety (non-negotiable)
- **Fixed sequence:** open, session check, Video tool, model, attach images, type, send, poll every 4 s, download.
- **Navigation:** top-level navigation only to gemini.google.com, accounts.google.com, consent.google.com and www.google.com.
- **No evasion:** no stealth, fingerprinting, proxies, CAPTCHA solving or account rotation.
- **Stops:** a limit, sign-out, verification/CAPTCHA or "upgrade required" page stops all generation with a clear message.
  Queued jobs fail with that message. Resume is manual (Settings → Resume generation).
- **Safety refusals** fail the job at once. The prompt is never reworded.
- **The browser profile is a credential:** owner-only (0700), never copied, backed up or moved. On a new machine, sign in again.
- **Logs** carry IDs, states, steps and durations only: never prompts, cookies, tokens or page text.

## Clip checks
- **Technical (always):** the file is a real container, decodes cleanly, is at least 7 s long and at least 360p.
  The wrong orientation only gives a warning.
- **Retries:** a failed check gets `MAX_RETRIES` more attempts, then the job fails.
- **Originals** are kept read-only at `data/clips/<job>/attempt-N.mp4`.

## Tests
`./run.sh test`: 64 tests covering auth and CSRF, the worker API (auth, size limits, loopback-only config),
the clip lifecycle with an FFmpeg test-pattern generator, retries, account stops, safety refusals, crash
recovery, profile locking, Gemini stop-text classification, the navigation guard, log hygiene, and the
Drive upload/verify/retry/Range flows (`httpx.MockTransport`). Test doubles live only in `tests/`.
