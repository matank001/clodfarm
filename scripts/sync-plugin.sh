#!/bin/sh
# The farm plugin (plugins/farm) carries its own copy of the hook's code: an installed plugin can't reach outside its
# folder. Run this after changing clodfarm/attach.py, sessions.py, scrub.py or the version; tests/test_attach.py
# fails while the copy differs.
set -e
cd "$(dirname "$0")/.."
for f in __init__.py attach.py sessions.py scrub.py; do
  cp "clodfarm/$f" "plugins/farm/lib/clodfarm/$f"
done
python3 - <<'PY'
import json, re
v = re.search(r'__version__ = "(.+)"', open("clodfarm/__init__.py").read()).group(1)
p = "plugins/farm/.claude-plugin/plugin.json"
d = json.load(open(p))
d["version"] = v
open(p, "w").write(json.dumps(d, indent=2) + "\n")
PY
echo "plugins/farm is in sync with clodfarm $(grep __version__ clodfarm/__init__.py | cut -d'"' -f2)"
