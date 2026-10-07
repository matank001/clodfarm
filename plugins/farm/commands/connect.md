---
description: Connect this computer to your farm (a browser page, once); this session goes on the farm
argument-hint: "https://<your farm> [a name for this computer]"
disable-model-invocation: true
---
<!-- clodfarm attach -->
The farm plugin's hook didn't take this command: it needs python3, which this computer may not have. Tell the user in
one sentence that the farm plugin needs python3 (on a Mac, `xcode-select --install` provides it). Do nothing else.
