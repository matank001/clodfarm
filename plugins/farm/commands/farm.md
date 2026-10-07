---
description: Put this session on the farm (/farm), take it off (/farm off), connect this computer (/farm connect URL), or see which (/farm status)
argument-hint: "[off | status | connect URL [name] | folder [off] | everywhere [off] | sign-out]"
---
<!-- clodfarm attach -->
The farm plugin's hook didn't take this command: it needs python3, which this computer may not have. Tell the user in
one sentence that the farm plugin needs python3 (on a Mac, `xcode-select --install` provides it). Do nothing else.
