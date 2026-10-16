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
