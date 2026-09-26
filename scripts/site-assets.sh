#!/bin/sh
# The landing page runs the farm UI's own renderer. Copy it (and its fonts) into site/ before deploying.
set -e
cd "$(dirname "$0")/.."
cp clodfarm/ui/app.js site/farm.js
cp clodfarm/ui/fonts/PressStart2P.ttf clodfarm/ui/fonts/VT323.ttf clodfarm/ui/fonts/OFL-*.txt site/fonts/
echo "site/farm.js and site/fonts/ updated from clodfarm/ui"
