#!/bin/bash
# The hand bot's setup on a new Mac or Linux computer, in one go: ./handbot/setup.sh (from the project folder; safe to
# run again). Installs everything, writes the bot's settings to .env, then checks it all. On a Mac it opens the two
# privacy pages to switch on; on Linux it installs Tesseract, xdotool and xclip (asks for your password: sudo).
# Windows: handbot\setup.ps1 instead.
set -e
cd "$(dirname "$0")/.."

if [ "$(uname)" = "Linux" ]; then
  missing=""
  for p in tesseract xdotool xclip; do command -v "$p" >/dev/null || missing="$missing $p"; done
  python3 -c "import venv" 2>/dev/null || missing="$missing venv"
  if [ -n "$missing" ]; then
    echo "Installing the Linux packages the bot needs…"
    sudo apt-get update -q
    sudo apt-get install -y -q tesseract-ocr xdotool xclip python3-venv python3-tk python3-dev
  fi
  # the X screen library the mouse and keyboard go through won't start without this file, even an empty one
  [ -f ~/.Xauthority ] || touch ~/.Xauthority
fi

echo "Installing…"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements-bot.txt

touch .env
if ! grep -q '^BOT_API_KEY=.\{24,\}' .env; then
  read -r -p "The app's address as this computer reaches it (Enter for the app on this computer): " url
  read -r -p "BOT_API_KEY (the same as the app's): " key
  [ -n "$url" ] && echo "APP_URL=$url" >> .env
  echo "BOT_API_KEY=$key" >> .env
  echo "Saved to .env"
fi

if [ "$(uname)" = "Darwin" ]; then
  if ! .venv/bin/python -m handbot check | grep -q 'terminal'; then
    echo "Permissions: done."
  else
    echo "Switch on your terminal app in the two pages that open (Accessibility, Screen Recording), then restart the"
    echo "terminal and run this again."
    open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
    open "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
    exit 0
  fi
elif [ -z "$DISPLAY" ]; then
  echo "No screen (DISPLAY isn't set): start the virtual screen first (handbot/README.md, \"On a Linux server\"),"
  echo "then run this again in it, such as:  DISPLAY=:1 ./handbot/setup.sh"
  exit 0
fi

echo
echo "Open Flow in Chrome (signed in), then press Enter to check the buttons on the screen…"
read -r
.venv/bin/python -m handbot check
echo
echo "Start the bot with:  .venv/bin/python -m handbot run"
