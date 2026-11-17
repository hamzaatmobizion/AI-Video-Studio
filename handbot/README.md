# Hand bot

Makes **by-hand** Google Flow videos of any length on a Mac of its own, one 8-second clip per scene. All of a video's scenes are made in one Flow project, one after another: each scene after the first gets the previous clip's last frame among its pictures (the app adds it), so the scenes join smoothly. Pictures already uploaded to the project, such as the characters from scene 1, aren't uploaded again: the bot types the start of the picture's name in the picker's **Search assets** box and picks it there. Only new pictures (the last frame, a character who first shows up in this scene) are uploaded. Each picture is named by its content ("3fa2c1-boy-stude.jpg"), so the same character has the same name in every scene. After each scene's upload the bot clicks your app's tab and opens the video's by-hand page, where the next scene is shown waiting ("The bot is making scene 2 of 3…"); after the rest between clips it clicks the Flow tab (`flow_tab`) and makes that scene in the same project. After the last scene it shows the finished video on the app's Flow page. If the project isn't showing any more when the next scene's turn comes (or after a stop and Try again), it starts a new project for it; the last frame still carries the scene on. It uses your normal Chrome, moves the real mouse and types on the real keyboard. There's no Playwright and no automation flags.

For each scene that's waiting, the bot does this:
1. It takes the prompt and pictures from the app.
2. For a video's first scene, it searches Google for Flow in its tab (a different search each time) and opens it, then starts a new project. It opens a new tab only the first time.
3. It picks Video, the aspect ratio, 8s, x1 and the model. Steps 3 and 4 (the settings, the pictures, pasting the prompt) are done in a random order for each clip; Generate always comes last.
4. It adds each picture: **+** opens Flow's picker, **Upload media** opens the Mac file dialog (the path is pasted after ⌘⇧G), then the upload, first in the picker's list, is picked and **Add to prompt** clicked. If the picker is still open afterwards (Flow ignores the click while it's still processing the upload), it clicks again, up to 4 times. It checks the pictures arrived (the prompt bar's ✕ shows), then pastes the prompt.
5. It clicks Generate, waits for the clip and downloads it.
6. It uploads the clip to the app. The app joins the clips and saves the video to Google Drive.
7. It clicks your app's tab in Chrome's tab bar (found by the app's purple ▶ tab icon), opens the app's Flow page in it (whatever page the tab was on) and scrolls down to the new video. With no tab of the app open, it opens the app's Flow page in a new tab, using macOS's control of Chrome's tabs: the first time, allow your terminal to control Google Chrome.

Chrome stays open and the bot never closes a tab; close Chrome yourself when you want.

While it runs, the Mac's screen is kept awake (macOS's `caffeinate`), so it doesn't sleep or lock.

Push the mouse into any corner of the screen to stop the bot at once (this is PyAutoGUI's fail-safe).

## Setup on the bot Mac
1. Install everything:
   ```
   python3 -m venv .venv && .venv/bin/pip install -r requirements-bot.txt
   ```
2. Create a `.env` file:
   ```
   APP_URL=http://<app host>:8000
   BOT_API_KEY=<same as the app>
   ```
   On the app side, set `BOT_API_KEY` and `BOT_USER_EMAIL`, the account whose videos the bot makes.
3. Allow the bot to control the Mac. In **System Settings → Privacy & Security**, turn on **Accessibility** and **Screen Recording** for your terminal app, then restart the terminal.
4. Set up Chrome:
   - Sign in to Google and to Flow by hand once.
   - Keep the zoom at 100%.
   - Keep Chrome on the main display with the same window size every time.
   - In Settings → Downloads, turn off "Ask where to save each file".
5. Take a picture of each button, one at a time:
   ```
   .venv/bin/python -m handbot capture new_project
   ```
   - The bot counts down 5 seconds. Switch to Chrome (⌘Tab) and show the button: open the settings panel, hover a tile for its ⋮, or open the ⋮ menu for Download. Add `--delay 10` for more time.
   - It takes a silent screenshot and opens it in a window.
   - Drag a tight box round the button and press **Enter**. Esc cancels.
   Repeat for every button in the table below.
6. Check the setup with `.venv/bin/python -m handbot check`. It lists every button picture and says which ones are on the screen right now.

| Button | Needed | What to capture |
|---|---|---|
| `search_result_flow` | yes | in Google's results for "google flow", the grey `https://flow.google.com` line under "Google". Not the title, which turns purple once you've visited it |
| `new_project` | yes | "+ New project" on Flow's home page, just the + and the words. The button is see-through over a project picture that changes, so the bot matches only its letters |
| `prompt_box` | yes | "What do you want to create?" in the empty prompt box |
| `add_image` | yes | the **+** at the left of the prompt bar. The bot searches only the bottom of the screen, so Library's **+** is never clicked |
| `settings` | yes | "Video · 720p · 8s" on the chip left of the →. Leave out the aspect icon and x1 |
| `generate` | yes | the **→** at the right of the prompt bar, after pasting a prompt (it turns into a white circle then). It's matched white or dark |
| `opt_video`, `opt_ingredients` | yes | Video and Ingredients in the settings panel, each with its icon |
| `aspect_9_16`, `aspect_16_9` | yes | 9:16 and 16:9 with their icons |
| `model_option` | yes | the text "Veo 3.1 - Lite" (without the ▾) |
| `model_menu` | yes | the ▾ arrow of the model menu |
| `opt_x1` | yes | x1 |
| `generating` | yes | the **%** sign of the progress ("25%") on a tile while its clip is being made. Just the %, not the number. The bot finds the new clip by its % (nothing else on the page shows one) and knows it's done once the % has gone; the ▶ can't tell, because it shows while the clip is still being made too |
| `tile_more` | yes | the ⋮ in the light bar that shows at the top of a tile while the mouse is over it (just the ⋮). The bot looks for it only next to where the new clip's % was, so the ⋮ buttons in Flow's header are never clicked |
| `download` | yes | "Download" with its icon, in the menu that ⋮ opens (without the ▸) |
| `download_720p` | yes | "720p / Original size" in the list of sizes that Download opens. The bot always downloads this size |
| `failed` | yes | the red "Failed" mark on a refused clip |
| `failed_refund` | no | on a failed clip, the words saying its credits will be refunded. With this picture, such a clip is generated again: the bot clears the prompt bar, adds the pictures again, pastes the prompt and clicks Generate, up to `REFUND_RETRIES` (2) times |
| `failed_retry` | no | the ↻ at the bottom of a failed clip. With it, about half the time a refunded clip is made again with its own ↻ instead of filling in the prompt bar again |
| `upload_media` | yes | "⤒ Upload media" at the bottom left of the picker that **+** opens |
| `picker_all` | yes | "All" with its grid icon, top left in that picker (the newest upload is found from it) |
| `add_to_prompt` | yes | the white "Add to prompt" button in that picker |
| `prompt_clear` | yes | the ✕ at the top right of the prompt bar: shows once a picture is in it, and clears it before a retry |
| `flow_tab` | no | Flow's icon and "Google Flow" in a Chrome tab's title. With it, the bot switches to an open Flow tab instead of opening a new one |
| `app_tab` | no | your app's tab icon, the purple ▶. It's the app's own icon (`static/img/favicon.png`), copied here, so there's nothing to capture |
| `flow_home` | no | the ⌂ home icon at the top left of a Flow project. With it, the bot sometimes goes to Flow's home page instead of searching |
| `agent_on` | no | the Agent pill while it's ON. The bot clicks it once to switch Agent off |
| `got_it`, `i_agree`, `opt_8s`, `opt_720p`, `captcha`, `signed_out` | no | each one is clicked or checked only if you've captured it |

The settings options are matched whether they're selected (white) or not (dark), so you can capture them in either state. Capture the other buttons without the mouse over them. If Flow's look changes, capture that button again.

If a Chrome tab has Flow open, the bot uses that tab: it clicks it in the tab bar (`flow_tab`) if it isn't the one in front, and never opens a second Flow tab. Then it goes straight to Flow's home page there (clicking ⌂ on a project page, `flow_home`) and starts a new project, without searching. Only when no Flow tab is open is Flow opened by searching Google and clicking Flow's result. The bot never types Flow's address. Each time it picks one search at random from `SEARCHES` in `flow.py` ("google flow", "googleflow", "flow google", "google labs flow" and others). About 4 times in 10 it types only the start of the search and takes one of Chrome's suggestions with the arrow keys. If that doesn't end on Google's results with Flow in them, or on Flow itself, it scrolls down once, then searches "google flow" the plain way.

## Run
```
.venv/bin/python -m handbot run
```
In the app, create a Flow video (any length) with **By hand** ticked, and press **Create Video** on its by-hand page. The bot picks it up within `POLL_SECONDS` and makes every scene; between scenes it rests as below. Clips are saved in `handbot-data/clips/`, and a screenshot of each step that matters goes in `handbot-data/logs/`.

Between clips it rests like the worker does: `MIN_GAP_SECONDS` (60) × 1 to 1.5, with at most `MAX_PER_HOUR` (12) an hour.

The bot stops on a video, tells your app why and switches Chrome to your app, when any of these happens. The video's page and card then show **The bot stopped: …** with **Try again** (once you've sorted it out in Chrome) and **Make it myself**. Meanwhile the bot carries on with any other videos given to it:
- Flow shows **Failed** (for example the "unusual activity" flag)
- a CAPTCHA appears (the bot never solves one: you do, then press Try again)
- Chrome is signed out
- it can't find a button

A Mac notification with a sound tells you it stopped. Pop-ups that only ask you to agree ("I agree", "Got it", "Accept", "OK"…) are read off the screen with macOS's text recognition and clicked, except any that mention payment, credits or a plan: those stop the bot.

The video stays waiting in the app: press **Try again** there and the bot takes it again, or **Make it myself** to make the clip on its by-hand page.
