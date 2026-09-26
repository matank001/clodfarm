#!/bin/sh
# Render scripts/architecture.html (the farm UI's own sprites) to assets/architecture.png with headless Chrome.
set -e
cd "$(dirname "$0")/.."
CHROME=${CHROME:-"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"}
"$CHROME" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 --window-size=1980,944 \
  --allow-file-access-from-files --virtual-time-budget=3000 --screenshot="$PWD/assets/architecture.png" \
  "file://$PWD/scripts/architecture.html" 2>/dev/null
echo "assets/architecture.png"
