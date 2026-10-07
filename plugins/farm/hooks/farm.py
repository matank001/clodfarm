"""The farm plugin's hook: Claude Code calls it with each event's JSON on stdin. The code is lib/clodfarm/attach.py,
a copy of clodfarm's own (scripts/sync-plugin.sh), so the plugin needs nothing installed but python3."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
from clodfarm.attach import run_hook  # noqa: E402

sys.exit(run_hook())
