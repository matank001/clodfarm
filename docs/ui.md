# The farm UI

`clodfarm run` serves a small web UI on port 8080 (`FARM_UI_PORT`; `FARM_UI=0` turns it off; `clodfarm ui` serves it
alone). Compose publishes it on `127.0.0.1:8080`, so it is reachable from the machine itself only. To reach it from
elsewhere, put a TLS reverse proxy in front (`FARM_UI_BASE=/team` serves it under a path, `FARM_UI_TRUST_PROXY=1`
reads the client address from `X-Forwarded-For` for the login lockout, `FARM_UI_SECURE=1` or `X-Forwarded-Proto: https`
marks the cookie Secure), or use an SSH tunnel: `ssh -L 8080:localhost:8080 myserver`.

## What you see

| On the farm | What it is |
|---|---|
| A Claude critter | One Claude: one person's account. The hat tells them apart. |
| At a terminal by a crop plot | It has sub-agents at work; they stand around its plot. |
| A mini Claude | One of its sub-agents, tinted with the colour of the Claude whose account runs it (a sub-agent can run on a teammate's budget). It types while it works and shows `…` while it waits for budget. |
| Napping by the barn (`Zzz`) | Resting: its budget governor is pacing this account. |
| `‖` | The farm is paused. |
| `!` | Its `clodfarm run` isn't running, or reported an error. |
| An egg with `?` | A Claude that isn't logged in yet. Click it to log it in. |
| Crops | They grow while a Claude's sub-agents work; finished work blooms into Claude's spark, failed work wilts. |

At night (your local time) the farm gets dark and the terminals glow.

Click a Claude for its status, its budget left (5-hour and 7-day, measured the moment it logs in and kept current),
its sub-agents (and the ones it runs for others), and **TALK TO IT**: the link to its Remote Control session in the
Claude app. That's where you give it work. Under **SESSIONS** are its conversations and sub-agent runs; open one to
read the whole conversation (every session is recorded by the farm's Claude Code hook). Click a mini Claude for that sub-agent's job, where it runs and its own
sub-agents. **+ NEW CLAUDE** (key `C`) adds a Claude login.

When you **release** a Claude, it leaves with everything it was running: its `clodfarm run` stops, its sub-agents go
back to wait for another Claude with budget, and it no longer shows on the farm.

## Adding Claudes ("hatching")

The container's own login is the farm's first Claude. **+ NEW CLAUDE** (or an egg on the farm) adds another one:

1. The UI creates a Claude config dir for it (`~/.claude/clodfarm-agents/<name>`, inside the claude-home volume, so
   the login survives a new container) and starts `claude auth login` for it in a pseudo-terminal.
2. You open the login link it prints, approve, and paste the code back into the UI. clodfarm types the code into
   Claude Code and never stores or logs it.
3. A child `clodfarm run` starts with that config dir and `FARM_NAME=<name>`: its own Remote Control session (it
   shows up in that person's Claude app under Code), the same repo and farm, and the governor paces it on that
   account's own usage (a new *seat*, see [multi-seat.md](multi-seat.md)). Its usage is measured right away.

Log in with a different Claude account for each person. **Release** stops that Claude (its sub-agents wait for
another Claude with budget), logs it out and deletes its config dir.

The token and API key from the container's environment (`CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`) are never
passed to hatched Claudes: each one uses only its own login.

## Password

One password, no username.

- `FARM_UI_PASSWORD` in `.env` wins; or
- `docker exec -it clodfarm clodfarm ui-passwd` sets a stored one (and signs every session out); or
- with neither, a random password is generated on first start and printed once in `docker logs clodfarm`.

Stored as a PBKDF2-SHA256 hash (600,000 rounds) in `/workspace/.farm/ui-auth.json` (mode 600). Sessions are
HMAC-signed, `HttpOnly`, `SameSite=Strict` cookies that last 7 days. Every write needs a JSON body and an
`X-Clodfarm: 1` header, five wrong passwords lock that address out for five minutes, and pages are served with a
strict Content-Security-Policy and `frame-ancestors 'none'`.

Anyone with the password can run agents on your Claude accounts and read your repo: treat it like an SSH key.
