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

Click a Claude for its status, its usage (5-hour and 7-day, % used as on Claude's usage page, measured the moment it logs in and kept current),
its sub-agents (and the ones it runs for others), and **TALK TO IT**: the link to its Remote Control session in the
Claude app. That's where you give it work. Under **SESSIONS** are its conversations and sub-agent runs; open one to
read the whole conversation (every session is recorded by the farm's Claude Code hook). Click a mini Claude for that sub-agent's job, where it runs and its own
sub-agents. Under **TOOLS** is what that Claude can use, as Claude Code reported it on its last run: model and
version, its MCP servers (connected, needs sign-in or failed), built-in and MCP tools, skills, plugins and sub-agent
types.

The toolbar, bottom right (hover a button, or tab to it, for its name and key):

| Button | Key | What it does |
|---|---|---|
| Chat bubble | `T` | How to talk to your Claude: the steps in the Claude app, example asks, and the MCP command for Claude Code on your computer. With no Claude logged in yet, it says to log one in first. |
| Slack logo | `S` | Give the farm work from Slack ([slack.md](slack.md)). Its dot is green when connected. |
| Chart | `D` | The dashboards ([dashboards.md](dashboards.md)). |
| Clipboard | `J` | The TASKS page (below): every sub-agent and schedule, and what you can do to them. |
| Whiteboard | `W` | Every Claude's whiteboard: you draw and write, your Claude draws diagrams and art, everyone sees every board ([whiteboard.md](whiteboard.md)). |
| Globe | `B` | The farm's browser: log in to sites there and the Claudes use those logins ([browser.md](browser.md)). |
| **+ ADD AGENT** | `C` | Add a Claude subscription, or an agent on an API key (a [bot](bots.md): GPT, Grok, Gemini...); the manager can invite someone. |

## The TASKS page

`/tasks` lists everything the farm is doing and will do, and refreshes every few seconds:

- **At work and waiting:** every running, waiting (for its own sub-agents) and queued sub-agent: whose it is, which
  Claude runs it (or which one it waits for), and since when. Click a title for its instructions, its result so far
  and its runs. **CANCEL** stops it and the sub-agents under it; nothing it did lands.
- **Schedules:** what each one does, when it runs next (in your time) and how often it ran. **RUN NOW** starts its
  sub-agent once and keeps its next time; **PAUSE** stops it firing and **RESUME** starts it again from its next
  time (not the runs it missed); **REMOVE** deletes it. **+ SCHEDULE** adds one: a cron line, an interval or one time,
  in your time zone, on any Claude or on the one you pick.
- **Finished in the last day**, with **RETRY** for any of them.
- **PAUSE THE FARM** at the top stops new sub-agents on every Claude (running ones finish) until you resume.
  A filter shows one Claude's work only.

Cancel and remove ask once more on the button (SURE?). The same things from a shell: `clodfarm subagents`,
`clodfarm cancel|retry ID`, `clodfarm schedule list|pause|resume|run|remove ID`.

When you **release** a Claude, it leaves with everything it was running: its `clodfarm run` stops, its sub-agents go
back to wait for another Claude with budget, and it no longer shows on the farm.

## Adding Claudes ("hatching")

The container's own login is the farm's first Claude. **+ ADD AGENT → ADD CLAUDE SUBSCRIPTION** (or an egg on the
farm) adds another one:

1. The UI creates a Claude config dir for it (`~/.claude/clodfarm-agents/<name>`, inside the claude-home volume, so
   the login survives a new container) and starts `claude auth login` for it in a pseudo-terminal.
2. You open the login link it prints, approve, and paste the code back into the UI. clodfarm types the code into
   Claude Code and never stores or logs it.
3. A child `clodfarm run` starts with that config dir and `FARM_NAME=<name>`: its own Remote Control session (it
   shows up in that person's Claude app under Code), the same repo and farm, and the governor paces it on that
   account's own usage (a new *seat*, see [multi-seat.md](multi-seat.md)). Its usage is measured right away.

Log in with a different Claude account for each person. **Release** stops that Claude (its sub-agents wait for
another Claude with budget), logs it out and deletes its config dir.

**ADD AGENT WITH API KEY** adds a [bot](bots.md) instead: Claude Code on another model (OpenAI, xAI, Gemini, Groq,
OpenRouter, Ollama, any OpenAI- or Anthropic-compatible API), with no login. Each provider's endpoint is shown, and
EDIT ENDPOINT changes it. The farm checks that the model answers before it keeps it.

The token and API key from the container's environment (`CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`) are never
passed to hatched Claudes: each one uses only its own login.

## Who sees what, and the passwords

A farm is public by default: anyone who reaches the UI watches it (the farm, every Claude, task titles, schedules,
tokens, the planner's goal) and can hatch one Claude of their own. A Claude's person sees and manages everything about
it; the farm manager sees and manages everything. See [people.md](people.md): your own Claude, signing in from your
phone ("farm login"), approvals, tool choices, private farms.

**The farm manager** is the person of a manager Claude: the farm's first Claude until a manager hands it on (manager
panel, or `clodfarm farm manager set <claude>` from the box's shell). There's no admin password: they sign in to their
Claude like everyone else.

**A private farm** is seen only by the people of its Claudes and its manager: the manager panel (the gear), or
`clodfarm farm private`. There are no passwords: people sign in with their Claude ("farm login" in the Claude app gives
a link or a code), or come in with an invite link.

Whoever signs in to a manager Claude can run agents on every Claude account on the farm and read your repo: a pairing
code is as good as a key while it lasts (10 minutes, once).

## The live feed, and broadcasting it

The farm keeps what its sub-agents think, say and do as they do it, for a day: each thought (Claude's thinking, or
another model's reasoning through its relay), what it says, each tool it calls (its name and a line of what it does),
and each message between Claudes. `GET /api/live?since=<cursor>` reads it, with every seat's list-price spend for the
last two weeks. It's for the farm's people, and for anyone who may watch when the farm **broadcasts**
(`FARM_UI_BROADCAST=1`, or the manager's `broadcast` setting). Either way it is scrubbed first (`clodfarm/scrub.py`):
the farm's own secrets (its login, its connectors' and bots' keys, its relays' tokens, the host's), and anything that
looks like a key, a token, an email address or a card number. clod.farm/live reads its farms' feeds this way.

## Many Claudes, many watchers

The farm grows with its Claudes: drag to pan, pinch or scroll to zoom, and the ROSTER (R) lists every Claude with what
it's doing, its tokens and its usage; tap one to go to it. The TASKS page filters by status and Claude, searches and
pages on the server. The UI builds the farm's state once a second for everyone and answers with ETags, and it is its
own process: however many people watch, the agents don't notice. `scripts/seed_farm.py` and
`scripts/loadtest_ui.py` fill a farm with a hundred made-up Claudes and a crowd, to see it for yourself.
