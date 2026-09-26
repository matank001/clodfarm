# Security model

clodfarm gives autonomous agents a shell. Be deliberate about what that shell can reach.

## What the agents can do

- By default they run with `--permission-mode bypassPermissions` **inside the container**, as the unprivileged
  user `farm`. They can read and write anything that user can, run any program in the image, install packages in
  their home directory, and reach the network.
- They can read the Claude Code config volume, which includes your login. The farm guide tells them never to print
  or copy credentials, but **a prompt is not a security boundary.** A prompt injection (a malicious README, issue or
  web page) could try to make an agent leak what it can read.

## Recommendations

1. **Mount only what the agents may change.** By default that's `/workspace` and the Claude home volume. Don't
   mount the Docker socket, your home directory or cloud credentials.
2. **Least-privilege credentials.** Give git a deploy key for one repo, not a personal token. On AWS the instance
   role can touch only the farm's own table.
3. **Stricter permission modes** if the work allows:
   - `FARM_PERMISSION_MODE=auto`: Claude Code's safety classifier decides, and anything it would ask about is
     denied (nobody is there to answer).
   - `acceptEdits` or `dontAsk`, combined with `permissions.allow` / `deny` rules in
     `/home/farm/.claude/settings.json`.
4. **Network egress:** the agents need `api.anthropic.com`, `claude.ai`, your git host and whatever your work
   needs. Tighten the rest with a firewall or an egress proxy if the work is sensitive.
5. **Review before trusting.** With `FARM_PUSH=1` the farm pushes `main`. Point it at a branch-protected repo
   that requires review, or at a fork, if unreviewed code on main is unacceptable.
6. **Token hygiene:** if you use `CLAUDE_CODE_OAUTH_TOKEN`, keep `.env` out of git and prefer a secret store.
   Revoke tokens you no longer use.

## What clodfarm itself does and doesn't do

- It never reads your credentials. It runs `claude auth status` and prints what that reports: logged in or not, the
  plan type, the email.
- The only network calls it makes itself go to DynamoDB. Everything else is Claude Code and the agents.
- It doesn't send messages, spend money, post publicly or create accounts. The guide tells agents not to either,
  unless the person they work for explicitly asks.

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md).
