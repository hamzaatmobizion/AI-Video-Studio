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

## Google Flow (second screen)
The **Flow** screen (`/flow`) is the same Studio (prompt, duration, characters, review, lock characters, clip
checks), but every clip is made in **Google Flow** (labs.google/fx/tools/flow) with **Veo 3.1 - Lite**, paid with
your Flow credits. You pick the platform per video: Studio makes Gemini videos, Flow makes Flow videos, and each
screen lists only its own.
- **8-second scenes.** Flow clips are 8 s, so durations go in 8-second steps (8 s to 1:36). Gemini's chat still
  writes the script, in 8-second scenes. That uses none of Gemini's daily video limit.
- **No Gemini quota.** With "Lock characters", the first scene's opening frame is drawn in Flow's Image mode
  (`Nano Banana 2.1`, `FLOW_IMAGE_MODEL_LABEL` to change it; one Flow image per video), not by Gemini, and later
  scenes start from the previous clip's last frame, as with Gemini.
- **Direct mode, one model, one clip.** Each clip opens a new Flow project, switches Flow's **Agent** toggle off (the
  prompt goes straight to the model, never through Flow's agent), and sets Video · `Veo 3.1 - Lite` · 720p · 8 s ·
  the aspect · **x1** (about 10 credits per clip). `FLOW_MODEL_LABEL` changes the model. If any setting can't be
  set, the clip fails rather than spend other credits.
- **Same sign-in.** Flow uses the worker's browser profile, so the Google account is Gemini's. Settings → Google Flow
  opens a Flow window and checks the session.
- **Pictures.** A scene with a first frame (the locked still, or the previous clip's last frame) starts exactly
  from it in Flow's *Frames* mode (the Start slot), so place, people and style carry on; the prompt then describes
  the characters in words, and what the prompt says about them (outfit, look) wins over their saved character. A
  scene without one gets the character pictures as *Ingredients* (**+ → Upload media**). The worker clicks **I agree**
  on Flow's "Rights to use this image" notice for you (approved by the account owner, like Gemini's upload consent).
  It agrees to nothing else: a CAPTCHA or "confirm it's you" check pauses Flow until you complete it by hand in the
  Flow window (Settings → Google Flow), then Resume Flow.
- **Separate pause.** Running out of Flow credits (or a Flow sign-out) pauses only Flow videos; Gemini keeps going.
  Gemini's video limit pauses only Gemini clips, so Flow videos still get their scripts. Resume each in Settings,
  or with Resume on the paused video.
- **One project per video.** A video's first Flow clip (or locked still) opens a new Flow project; its later clips and
  stills go back into that same project (cleared of the last clip's pictures), so a video adds one project to your
  Flow account, not one per scene. If it can't be reused, a new one is opened.
- **Calibrate the page.** `.venv/bin/python -m worker check-flow` is read-only: it loads Flow's home page and checks the
  sign-in. Only after Flow changes its page, `check-flow --project` also opens an empty project (no credits) to show
  which selectors in `worker/flow_ui.json` match. Keep these runs rare: every automated visit is activity on the
  account.

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
| `GEMINI_PLAN_MODELS` | `Flash,Pro` | Chat model(s) that write scene plans, comma-separated and tried in turn on retries; empty = keep the current one |
| `FLOW_MODEL_LABEL` | (unset; see `profile_flow.json`) | Model picked in Google Flow's menu, default `Veo 3.1 - Lite` |
| `FLOW_IMAGE_MODEL_LABEL` | (unset; see `profile_flow.json`) | Image model for a Flow video's locked first frame, default `Nano Banana 2.1` |
| `GENERATION_TIMEOUT_SECONDS` | `900` | Maximum wait for Gemini to finish one clip |
| `MAX_RETRIES` | `1` | Extra attempts after a QC or Gemini error |
| `RETRY_DELAY_SECONDS` | `90` | Wait before a retry; other queued jobs run meanwhile |
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
POST   /videos          {"prompt","aspect","ref","provider"?,"frame"?} JSON, or multipart with up to 3 images → {"job_id"}
                          provider: gemini (default) | flow; frame: index of the image that is the start frame
POST   /plans           {"prompt","aspect","ref","scenes","cast"?,"style"?,"language"?,"rules"?,"clip_seconds"?}
POST   /stills          multipart: body={"description","aspect","names","previous"} + up to 4 images → {"job_id"}
POST   /checks          multipart: body={"names","previous","frames"} + pictures, previous frame, clip frames → {"job_id"}
GET    /videos/{id}     → {"status": queued|running|done|failed, "error", "video_url", "final_prompt"}
                          (+ "image_url" for portraits and stills, "check": {"ok","problems"} for checks)
GET    /videos/{id}/file → MP4 (Bearer)
DELETE /videos/{id}     → best-effort cancel
GET    /health          → {"account_paused", "pauses": {gemini, flow, chat}, "queued", "running", "gemini", "flow", ...}
GET    /gemini/status · POST /gemini/login · /gemini/login/done · /gemini/check · /gemini/resume
GET    /flow/status   · POST /flow/login   · /flow/login/done   · /flow/check   · /flow/resume
```
- `error` is human-readable and shown verbatim.
- A 4xx is a final failure. A 5xx or network error is retried on the next poll, and the job fails after 10 in a row.
- A job that hasn't finished after 30 minutes is cancelled.

## Account safety (non-negotiable)
- **Fixed sequence:** open, session check, Video tool, model, attach images, type, send, poll every 4 s, download.
- **Navigation:** top-level navigation only to gemini.google.com (Flow: labs.google), accounts.google.com, consent.google.com and www.google.com.
- **No evasion:** no stealth, fingerprinting, proxies, CAPTCHA solving or account rotation.
- **Stops:** a limit (Flow: out of credits), sign-out, verification/CAPTCHA or "upgrade required" page stops that
  provider's generation (Gemini or Flow) with a clear message.
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
