# The farm UI

`clodfarm run` serves a small web UI on port 8080 (`FARM_UI_PORT`; `FARM_UI=0` turns it off; `clodfarm ui` serves it
alone). Compose publishes it on `127.0.0.1:8080`, so it is reachable from the machine itself only. To reach it from
elsewhere, put a TLS reverse proxy in front (it should send `X-Forwarded-Proto: https`, which marks the cookie
`Secure`), or use an SSH tunnel: `ssh -L 8080:localhost:8080 myserver`.

## What you see

| On the farm | What it is |
|---|---|
| A Claude critter | One worker (`w0`, `w1`, ...) of one agent. The hat tells agents apart. |
| At a terminal by a crop plot | Running that task. Hover it to read the title, click it for the details. |
| Napping by the barn (`Zzz`) | Throttled: the budget governor is pacing this account. |
| `‖` | The farm is paused. |
| `!` | The worker reported an error, or the agent's process isn't running. |
| An egg with `?` | An agent that isn't logged in yet. Click it to log it in. |
| Crops | Running tasks grow; done tasks bloom into Claude's spark; failed ones wilt. |
| The quest board | The queue: one note per queued task. |
| A mini Claude | A sub-agent: a sub-task of the quest on that plot. It types when it's working and shows `…` while it waits for a free Claude. |

At night (your local time) the farm gets dark and the terminals glow.

Click a Claude for its status, what it's working on, its sub-agents, its quests of the last 7 days and its budget
left (5-hour and 7-day). Click a plot for its quest. **+ NEW CLAUDE** adds a Claude login, **NEW QUEST** posts a
quest (just text: its first line becomes its name), and **GOAL** sets MISSION.md. Keys: `N` new quest, `C` new
Claude, `M` mission.

## Agents ("hatching")

The container's own login is the primary agent. **+ NEW CLAUDE** (or an egg on the farm) adds another one:

1. The UI creates a Claude config dir for it (`~/.claude/clodfarm-agents/<name>`, inside the claude-home volume, so
   the login survives a new container) and starts `claude auth login` for it in a pseudo-terminal.
2. You open the login link it prints, approve, and paste the code back into the UI. clodfarm types the code into
   Claude Code and never stores or logs it.
3. A child `clodfarm run` starts with that config dir and `FARM_NAME=<name>`. It joins the same queue and repo, and
   the governor paces it on that account's own usage (it is a new *seat*, see [multi-seat.md](multi-seat.md)).

Log in with a different Claude account to add capacity. The same account a second time adds workers but shares one
budget. **Release** stops the agent (its running task goes back to the queue), logs it out and deletes its config dir.

The token and API key from the container's environment (`CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`) are never
passed to hatched agents: each one uses only its own login.

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
