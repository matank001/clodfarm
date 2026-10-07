#!/bin/sh
# The farm plugin (plugins/farm) carries its own copy of the hook's code: an installed plugin can't reach outside its
# folder. Run this after changing clodfarm/attach.py, sessions.py, scrub.py or policy.py; tests/test_attach.py fails
# while the copy differs. Then bump the version in plugins/farm/.claude-plugin/plugin.json: installed copies update
# only when it changes.
set -e
cd "$(dirname "$0")/.."
for f in __init__.py attach.py sessions.py scrub.py policy.py; do
  cp "clodfarm/$f" "plugins/farm/lib/clodfarm/$f"
done
python3 - <<'PY'
import json, sys
sys.path.insert(0, ".")
from clodfarm.attach import hook_spec
spec = {"description": "Reports a connected session to the farm, applies its Claude's tool settings, wakes it for "
                       "the farm's messages, and takes /farm:<command>",
        "hooks": hook_spec('python3 "${CLAUDE_PLUGIN_ROOT}/hooks/farm.py"')}
open("plugins/farm/hooks/hooks.json", "w").write(json.dumps(spec, indent=2) + "\n")
PY
echo "plugins/farm is in sync; bump plugins/farm/.claude-plugin/plugin.json's version if the plugin changed"
