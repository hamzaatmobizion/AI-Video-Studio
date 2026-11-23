# Guide: your first 10-second video

1. **Start it.** In Terminal, in this folder, run `./run.sh`. Leave it running.
2. **Sign in to the app.** Open http://localhost:8000 (or your `APP_PORT`). The first time, click
   **Create one** under the Sign in button to make an account; it signs you in straight away.
3. **Connect Gemini (once).** Go to Settings and click **Open Gemini sign-in window**. A separate browser
   window opens. Sign in to the Google account that has Gemini video access, open Gemini, then close
   the window by clicking **Done signing in** in Settings. Status should say **Connected**.
4. **Connect Google Drive (once).** Settings, then **Connect Google Drive**, then approve. Videos go to the
   "VideoGeneration" folder unless you choose another.
5. **Make a video.** On the Studio page, describe the video, pick a **Length** (10 seconds to 2:00), pick
   9:16 (Reels) or 16:9, optionally add up to 3 reference images, and click **Generate**. For longer videos
   Gemini first writes the scenes ("Gemini is writing 6 scenes…"), then the card shows Generating scene 1
   of 6…, Joining scenes…, Saving to Google Drive…, then the player. To write each scene yourself, click
   **Write scenes myself**.

## If something goes wrong
- **"Gemini's video limit has been reached"**: the video pauses on purpose, with its finished scenes kept.
  It never restarts by itself: wait for the limit to reset, then click **Resume** on that video.
- **"Gemini is signed out" / "confirm it's you"**: open the sign-in window from Settings, finish the
  sign-in or check by hand, click Done, then Resume.
- **"declined this prompt for safety reasons"**: change the prompt yourself and use Retry.
- **"Your Google Drive is full"**: free space, then click **Try saving again**. The video isn't remade.
- **"Couldn't find Gemini's Video tool"**: Gemini changed its page. Run
  `.venv/bin/python -m worker check-gemini`, look at the screenshot in `data/diagnostics/`, and
  update `worker/gemini_ui.json`.
- **Google won't let you sign in in the window** ("browser may not be secure"): set
  `BROWSER_CHANNEL=chrome` in `.env` (uses your installed Google Chrome with its own separate profile)
  and sign in again.

## Terminal commands
Run these in Terminal, in this folder.

| Command | What it does |
|---|---|
| `./run.sh setup` | First time: makes `.venv`, installs everything, Playwright's Chromium, and a `.env` with fresh secrets |
| `./run.sh` | Starts the web app and the worker; Ctrl+C stops both |
| `./run.sh test` | Runs the tests |
| `./run.sh doctor` | Checks the web app's and the worker's setup |
| `./run.sh help` | Lists these commands |
| `.venv/bin/python -m worker login` | Opens the Gemini sign-in window (same as the button in Settings) |
| `.venv/bin/python -m worker check-gemini` | Shows which parts of Gemini's page are found, with a screenshot in `data/diagnostics/` |
| `.venv/bin/python -m worker check-flow` | Checks Google Flow's sign-in (add `--project` only after Flow changes its page) |
| `.venv/bin/python -m app.create_user` | Resets a forgotten password (same email, new password) |

## Moving to another device
1. Install Python 3.12+ and ffmpeg (`brew install ffmpeg`).
2. Copy this folder (commit and push first, or copy it as it is), then run `./run.sh setup`.
3. Fill in `.env`: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `PUBLIC_BASE_URL` (and add
   `PUBLIC_BASE_URL/auth/google/callback` as a redirect URI in Google Cloud Console), plus `BOT_API_KEY` and
   `BOT_USER_EMAIL` if you use the hand bot.
4. To keep your accounts and videos, copy `var/app.db` along with the old `.env`'s `SECRET_KEY` and
   `TOKEN_ENCRYPTION_KEY`. Without that key the saved Drive connection can't be read.
5. Never copy `data/browser-profile`: it holds your Google sign-in. Sign in to Gemini and connect Drive again in
   Settings instead.
6. Run `./run.sh`.

## Hand bot (on its own Mac)
Full details are in `handbot/README.md`.
1. **Set up:** `./handbot/setup.sh` from the project folder. It installs everything, asks for the app's address
   and `BOT_API_KEY` (the app's value) for `.env`, and opens **Accessibility** and **Screen Recording**: turn on
   your terminal in both, restart it and run `./handbot/setup.sh` again.
2. **Chrome:** sign in to Google and Flow by hand once. The setup's check says if Chrome asks where to save
   downloads or Flow isn't at 100% zoom.
3. **Buttons:** the pictures come with the project and are resized to any screen. The setup's check lists which
   ones are on the screen; capture one again only if it isn't found or Flow's look changed:
   `.venv/bin/python -m handbot capture <name>` (switch to Chrome during the 5-second countdown, drag a box round
   the button and press Enter).
4. **Run it:** `.venv/bin/python -m handbot run`. Push the mouse into a screen corner to stop it at once.
