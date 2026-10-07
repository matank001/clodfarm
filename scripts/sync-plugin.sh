#!/bin/sh
# The farm plugin (plugins/farm) carries its own copy of the hook's code: an installed plugin can't reach outside its
# folder. Run this after changing clodfarm/attach.py, sessions.py or scrub.py; tests/test_attach.py fails while the
# copy differs. Then bump the version in plugins/farm/.claude-plugin/plugin.json: installed copies update only when it
# changes.
set -e
cd "$(dirname "$0")/.."
for f in __init__.py attach.py sessions.py scrub.py; do
  cp "clodfarm/$f" "plugins/farm/lib/clodfarm/$f"
done
echo "plugins/farm/lib is in sync; bump plugins/farm/.claude-plugin/plugin.json's version if the plugin changed"
