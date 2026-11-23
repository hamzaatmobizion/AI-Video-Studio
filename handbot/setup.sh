#!/bin/bash
# The hand bot's setup on a new Mac, in one go: ./handbot/setup.sh (from the project folder; safe to run again).
# Installs everything, writes the bot's settings to .env, opens the two privacy pages to switch on, then checks it all.
set -e
cd "$(dirname "$0")/.."

echo "Installing…"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements-bot.txt

touch .env
if ! grep -q '^BOT_API_KEY=.\{24,\}' .env; then
  read -r -p "The app's address as this Mac reaches it (Enter for the app on this Mac): " url
  read -r -p "BOT_API_KEY (the same as the app's): " key
  [ -n "$url" ] && echo "APP_URL=$url" >> .env
  echo "BOT_API_KEY=$key" >> .env
  echo "Saved to .env"
fi

if ! .venv/bin/python -m handbot check | grep -q 'terminal'; then
  echo "Permissions: done."
else
  echo "Switch on your terminal app in the two pages that open (Accessibility, Screen Recording), then restart the"
  echo "terminal and run this again."
  open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
  open "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
  exit 0
fi

echo
echo "Open Flow in Chrome (signed in), then press Enter to check the buttons on the screen…"
read -r
.venv/bin/python -m handbot check
echo
echo "Start the bot with:  .venv/bin/python -m handbot run"
