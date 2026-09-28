# Changelog

## 0.9.0 (2026-09-28)

Bots on other models, a proxy for the farm's browser, a Claude you can talk to while its sub-agents work, and a
cancel that stops them.

- **The farm's browser page no longer comes up blank behind a proxy.** The page loads noVNC's ~55 modules at
  once, and a proxy in front (like clod.farm's) opens a connection for each: the UI server's listen backlog of 5 made
  Linux drop most of them, the proxy answered 502, and a module that fails to load stops the whole page (no profiles,
  no buttons, "…" at the top). The server now queues 256 connections, and the page loads noVNC only when it shows a
  screen, so its controls work even if the screen can't load (it then says to reload).
- **Bots: other models on the farm** (#9). A bot is Claude Code on another model, through any provider that speaks
  Anthropic's API: OpenRouter's free models, a local Ollama, a LiteLLM gateway. Add one in the farm UI (+ NEW CLAUDE,
  BOT: OTHER MODEL) or with `clodfarm bot add`: the farm asks the model for one word first and keeps the bot only if
  it answers. A bot uses no Claude account's usage. It takes only the sub-agents sent to it (`--on <bot>`, or any if
  you say so), its own sub-agents stay on it, and it pauses when its provider rate-limits it. The guide tells your
  Claudes to send it well-specified, low-risk jobs and check the result. Its key stays in its own config dir.
- **Talk to your Claude while its sub-agents work.** In a conversation from the Claude app, a Claude used to wait for
  each sub-agent with `clodfarm result <id> --wait` in the foreground: one tool call that blocked for up to 10 minutes,
  so everything you typed waited until that sub-agent finished. The guide (and `clodfarm spawn`'s output) now has it
  wait in the background and end its turn: your next prompt is answered at once, and Claude Code wakes it with the
  result when the sub-agent is done. Checked against the real Claude Code: a prompt sent while a sub-agent ran was
  answered in 2 s, and the conversation woke by itself with the result.
- **Cancel stops a running sub-agent.** It used to only change the task's status: the run went on, used budget and
  could still land its work on main. Now its box notices within 3 s and stops the run, nothing it did lands (also
  when the cancel comes while its check runs), a cancel isn't counted toward the circuit breaker, and cancelling a
  sub-agent cancels the ones under it.
- **A new farm UI password works at once.** `clodfarm ui-passwd` used to take effect only after a restart (the
  running UI kept the old password and sessions); now the UI picks the new one up, and signs old sessions out,
  immediately.
- **What you ask your Claude to tell a running sub-agent is your instruction.** A `clodfarm msg <id>` from a
  conversation to one of its own Claude's sub-agents is shown to it as its person's instruction, not as another
  Claude's request; `--urgent` says so too. Messages from other Claudes and from sub-agents stay requests.
- **A proxy for the farm's browser, from the country you pick.** A CONNECTION bar at the top of `/browser` sends
  each profile DIRECT or VIA PROXY, and says what sites see. The farm has one proxy address: SET PROXY takes
  `LOGIN:PASSWORD@HOST:PORT` (DataImpulse: `gw.dataimpulse.com`, a sticky port 10000–20000 for sites you're logged
  in to), and each profile picks its own country (DataImpulse's `__cr.<country>`, added to the login for it). Every
  switch is checked first and shows the exit IP and city. Chromium can't take a proxy login, so a relay on
  127.0.0.1 adds it; a wrong login is an error on the page, never a login prompt the Claudes would get stuck on.
  WebRTC can't go around the proxy, and a profile with the proxy on never falls back to going direct.
  `FARM_BROWSER_PROXY` sets the address instead; `clodfarm browser proxy on|off [PROFILE] [--country us]`.

## 0.8.0 (2026-09-27)

The farm's browser, a Claude that works in a conversation shows it on the farm, and a farm that stays bright.

- **The farm's browser.** One Chromium in the container that you see and use in the farm UI (the globe button, or
  B, at `/browser`): log in to a site there once (LinkedIn, an admin panel) and every Claude works in that login with
  its new `browser` MCP tools (Playwright attached over DevTools), in the window you watch. START turns it on for the
  box and it stays on across restarts; the profile lives in the workspace volume, so a new container is still logged
  in. The screen comes over the UI's own WebSocket behind the farm password (noVNC); paste and copy work, and ⌘
  shortcuts on a Mac. The guide tells the Claudes to use their own tab, never type passwords (they ask you to log in
  instead) and act on your accounts only as asked. **Profiles**: each its own Chromium with its own logins (two
  LinkedIn accounts: two profiles), added with + PROFILE, up to 8; any Claude can use any of them
  (`mcp__browser__*` for `default`, `mcp__browser-<name>__*` for the others).
  `clodfarm browser [start|stop [PROFILE] | open URL [--profile P] | add|remove NAME]`. The image gains
  Chromium, Xvfb, x11vnc, noVNC and Node; `--build-arg BROWSER=0` builds without them, `FARM_BROWSER=0` turns it off.
- **A Claude at work in a conversation shows it.** While a conversation's turn runs (you asked it something from
  the Claude app, in any session, new or old), its Claude gets a plot, grows crops and works at its laptop, with
  "TALKING: <the conversation>" over its head, like a Claude with sub-agents. The farm's hooks mark a conversation
  busy from its prompt until its turn ends (kept busy while a Stop keeps it going for mail); a turn whose transcript
  goes quiet for 10 minutes (interrupted, so no Stop came) counts as over. `/api/state` gives each Claude `talking`.
- **No night.** The farm no longer darkens in the evening: the blue night wash and the fireflies are gone.
- **Node and the AWS CLI on every box.** The image has Node 22 (npm, npx) and the AWS CLI v2 whatever the build
  args (Node came only with the browser before, and `aws` was downloaded at startup only with the apps role on). `aws`
  has no credentials unless you give the box some (the apps role does).
- **The guide explains the dashboards.** Every Claude's guide now has the dashboard spec and its widgets, says the
  page's link goes to the person, and tells them to use the farm's dashboards instead of building their own.

## 0.7.0 (2026-09-27)

Dashboards that show what is improving, each Claude's tools on its card, and an optional AWS role so the farm can
build and run its own apps.

- **Dashboards.** The Claudes build pages that show what is improving, at `/dashboards/<name>`, with a list at
  `/dashboards` (the chart button on the farm, or D). A dashboard is a JSON spec of widgets (stats, charts, bar lists,
  tables, progress, notes) that the farm draws in its own look; no agent-written code runs in the browser. Every push
  records each stat's value (one point per hour, kept 400 days), so stats show their trend and change over 24h, 7d,
  30d or 90d without the agent keeping any history. `clodfarm dashboard metric` logs one number; `push --run CMD
  --every 1h` makes a dashboard live: the farm runs that code in the repo on schedule (claimed once across boxes) and
  pushes what it prints. When a live dashboard breaks, the page says so and its Claude gets a message (`--wake`) to
  fix it. MCP: `farm_dashboards` and `farm_dashboard_push`.
- **Each Claude's tools.** Tap a Claude: its card lists what it can use, as Claude Code reported it on its last run:
  model and version, MCP servers and whether they're connected (or need sign-in, or failed), built-in and MCP tools,
  skills, plugins and sub-agent types.
- **A smaller toolbar.** Slack is now just its logo, and "talk to your Claude" is a small button (or T) that opens a
  window with the steps, example asks and the MCP command for Claude Code on your computer.
- **Optional apps role: the farm can build and run its own apps on AWS.** `deploy/aws/deploy.sh apps-role --email
  you@example.com [--budget 50] [--domain apps.example.com] [--regions ...]` deploys `deploy/aws/apps-role.yaml`
  (in its own account with `APPS_PROFILE`), lets the farm box assume it and restarts the farm with `FARM_AWS_APPS_*`.
  Every Claude then gets `aws --profile apps` and an AWS section in its guide. Fenced: serverless services only, a
  permissions boundary on every role it creates, no IAM users, keys, email or domain purchases, and an AWS Budget that
  locks the role at 100%. `apps-down` removes it. The plain deployment is unchanged and still gives no AWS access.
- The image is unchanged: with the apps role on, the farm installs the AWS CLI v2 into the farm user's home at startup.
- **The README in the clod.farm theme**: the site's farm scene as the hero, farm-sign section headings, the four steps
  as farm panels, a fresh screenshot of the new UI, and real icons (Slack, Claude and MCP marks, line icons) instead of
  emoji. `scripts/render-readme.sh` regenerates the images.

## 0.6.0 (2026-09-27)

Messages between the Claudes arrive in seconds instead of at the person's next prompt.

- **Claude Code's own messaging on the farm.** The guide teaches `ListAgents` and `SendMessage`: a message reaches a
  live session at its next tool call and wakes an idle one. Every Claude added in the farm UI lists its sessions
  with the farm's own Claude's (`FARM_SHARE_SESSIONS`), so they all see each other, and each takes messages without
  holding them for an approval nobody can give (`crossSessionInbound: accept`, unless set by hand). Sub-agent
  sessions are named `[clodfarm] <claude> · <title> · <id>`, and messages sent this way show up in the event log.
- **`clodfarm msg` to a sub-agent, and delivered while it works.** Address a Claude or a sub-agent id. A doorbell in
  the store tells the recipient's box at once; a `PostToolBatch` hook hands the mail over after its next batch of
  tool calls, the `Stop` hook before its turn ends, and an async hook (`asyncRewake`) wakes an idle conversation. A
  sub-agent that isn't running gets its mail with its prompt. Each message is delivered exactly once (it used to be
  possible twice), has an id and a reply address, and `clodfarm inbox` in a sub-agent reads its own mail.
- **`--urgent`** interrupts a running sub-agent and hands the message over at once: sub-agents now read stream-json on
  an open stdin (`FARM_LIVE_STDIN=0` goes back to a plain prompt).
- **`--wake`**: mail nobody read after `FARM_MAIL_WAKE_AFTER` seconds starts a sub-agent on that Claude's account, or
  resumes the finished sub-agent it was for. Capped by `FARM_MAIL_MAX_HOPS` and `FARM_MAIL_WAKES_PER_HOUR`, so two
  Claudes can't keep each other busy.

## 0.5.0 (2026-09-27)

- **Connect Claude Code on your computer: the farm is a remote MCP server** at `/mcp` (Streamable HTTP).
  `claude mcp add --transport http farm <url>/mcp`, then `/mcp` to sign in on the farm's own page (its password and a
  name for your computer). 15 tools: see the farm, spawn/cancel/retry sub-agents, message Claudes, read their
  answers, schedules. OAuth 2.1 with discovery (RFC 9728/8414), dynamic registration, PKCE S256, audience-bound
  hour-long tokens and rotating refresh tokens stored only as hashes; read-only or read-and-work. It can never log
  Claudes in or out, release them or pause the farm. `clodfarm connect | connections | disconnect ID`;
  `FARM_PUBLIC_URL` behind a proxy that rewrites Host. See docs/mcp.md.
- **Give the farm work from Slack.** DM the farm's app or @mention it in a channel: a sub-agent does the job and
  answers in the thread (👀 while it works, ✅ or ❌ when done). Follow-ups in the thread carry the conversation so
  far; `status` shows the Claudes and their usage. It runs on the sender's own Claude (matched by account
  email, stored only as the seat hash), else on a random Claude with room; `gil: …` runs it on gil's account. Connect it from the farm UI's
  new **SLACK** button in about two minutes: a prefilled Slack app manifest, then two tokens pasted back. It uses
  Socket Mode, so there's no public URL or open port, and the client is standard library only. Only full members of
  the workspace can use it (never guests or people from other organisations), optionally narrowed to an allow list.
  `clodfarm slack` prints the steps for a setup without the UI. See [docs/slack.md](docs/slack.md).
- `clodfarm msg` reaches connected Claude Codes by their name.
- **A 10-second demo at the top of the README** (`assets/demo-10s.webp`, click for the MP4), drawn with the farm UI's
  own sprites. Re-render it with `scripts/render-demo.mjs` (headless Chromium + ffmpeg); `scripts/demo10.html` is the
  deterministic timeline.

## 0.4.4 (2026-09-27)

- **Always the newest Claude Code.** The image ships the `latest` release instead of `stable`, and the farm updates
  it every hour (`FARM_CLAUDE_UPDATE`, 0 = never), so new models such as Opus 5.5 reach every Claude the day they ship.
  On `stable` (2.1.274), `opus` still meant Opus 5. Sub-agents run on a new version at once; each Claude's Remote
  Control restarts on it once no one has talked to that Claude for 15 minutes.
- **The sessions you open from the Claude app use `FARM_MODEL`** by default, like the farm's sub-agents: it is set
  as each Claude's default model.

## 0.4.3 (2026-09-27)

- The farm's own usage check is no longer recorded as a session, and `clodfarm sessions` hides the ones older
  versions recorded (`--all` shows them).

## 0.4.2 (2026-09-26)

- **Usage reads like Claude's own usage page:** "35% used", a bar that fills up to 100% (green, then amber, then red),
  and when it resets. Everywhere: the Claude card, `clodfarm agents`, the guide and the docs.
- **The farm's own Claude is named after its account,** not after the farm: on the farm `jestr`, the Claude logged in
  as matan@… is `matan` (set `FARM_CLAUDE_NAME` to choose). Its Remote Control session is `[clodfarm] jestr · matan`.
  Added Claudes keep the name you give them.

## 0.4.1 (2026-09-26)

- **Every Claude session and its whole conversation are in the farm's store.** A Claude Code hook (`clodfarm hook`,
  installed in each Claude's settings) runs on session start, each prompt, each reply and session end: it registers
  the session (which Claude, conversation or sub-agent, its task, its Remote Control session) and copies the new
  transcript turns (your messages, Claude's replies, tool calls and results in short; thinking is left out). Read
  them with `clodfarm sessions` / `clodfarm session ID`, or on each Claude's card in the farm UI. Messages from other
  Claudes are delivered at the next real prompt (never used up by a session that gets none).
- **Every farm session is marked `[clodfarm]`** in the Claude app and claude.ai/code: the Remote Control session
  (`[clodfarm] <name>`), the sessions you open from it, and each sub-agent (`[clodfarm] <name> · <job>`).

## 0.4.0 (2026-09-26)

One Claude per account, sub-agents you can see, and Claudes that work together.

- **The farm shows Claudes, not workers or a queue.** One critter per Claude (a person's account). The sub-agents
  it starts are mini Claudes around its plot, tinted with the colour of the Claude whose account runs them. Tap a
  Claude for its budget, its sub-agents and the link to talk to it; tap a sub-agent for its job and where it runs.
- **New CLI for the Claudes and you:** `clodfarm spawn` (start a sub-agent; any Claude with budget runs it, or
  `--on NAME`), `subagents`, `result ID [--wait]`, `cancel`, `retry`; `clodfarm agents` shows every Claude's 5-hour
  and 7-day budget left and how many more sub-agents it can start; `clodfarm msg NAME TEXT` and `clodfarm inbox`
  for messages between Claudes. New messages appear in a Claude's next conversation turn (a Claude Code hook the
  farm installs). The farm guide teaches budget, saving it by running sub-agents elsewhere, and collaboration.
- **Usage in real time:** a new Claude's usage is measured the moment it logs in, and an idle Claude is re-measured
  every `FARM_USAGE_REFRESH` seconds (300); runs keep reporting it live.
- **Removed:** the planner, `MISSION.md`, `clodfarm mission`, `FARM_PLANNER*`, `FARM_MISSION` and the
  `clodfarm task ...` commands (use `spawn` / `subagents` / `result`). `schedule add --to` is now `--on`.
- The README has a new architecture picture, drawn with the farm's own sprites (`scripts/architecture.html`).

## 0.3.0 (2026-09-26)

Simpler: you talk to your Claude, and it runs the farm.

- **No more quests, goal or mission in the UI.** The farm UI shows your Claudes at work; tap one for its budget and
  a **TALK TO IT** link to its Remote Control session in the Claude app. That's where you give it work.
- **Claudes work together:** `clodfarm agents` lists the Claudes on the farm, and `clodfarm task add ... --to gil`
  hands a task to one of them: only its boxes take it, on its own account's budget. Your Claude does this when you
  ask ("have gil's Claude review it").
- **Scheduled tasks:** `clodfarm schedule add TITLE --prompt ... (--cron "0 9 * * 1-5" --tz Europe/Berlin | --every 2h |
  --at "in 3h")`, `schedule list`, `schedule remove`. Every box checks every 15 s; each firing runs once.
- **The planner is opt-in** (`FARM_PLANNER=1` with a `MISSION.md`). `clodfarm mission` still works for it.
- **Fix: a released Claude left its workers behind.** The agent keeper could restart a Claude while it was being
  released, and its last heartbeats kept it on the farm as a "visitor". Now it leaves the registry first, can't be
  restarted, its running tasks go back to the queue (fresh, without waiting for its box), its slots are freed and
  its heartbeats dropped.

## 0.2.1 (2026-09-26)

- The farm UI runs behind a reverse proxy under a path prefix: `FARM_UI_BASE=/team` (the UI uses relative URLs, the
  cookie is scoped to the prefix), `FARM_UI_SECURE=1` for a Secure cookie, and `FARM_UI_TRUST_PROXY=1` so the login
  lockout counts the forwarded client, not the proxy. `/healthz` stays at the root for the container health check.

## 0.2.0 (2026-09-26)

- **The farm UI** (`http://localhost:8080`, served by the daemon; `FARM_UI=0` turns it off). A pixel-art farm: every
  worker is a Claude critter that wanders, tends its task's crop at a terminal, or naps when the governor paces it.
  Finished tasks bloom into Claude sparks, sub-agents are mini Claudes around their parent's plot, the quest board
  shows the queue. Click a Claude for its budget and stats; post a quest as plain text.
- **clod.farm** runs the same farm (a scripted demo) behind the install card.
- **Add Claudes from the browser.** Each agent you add is its own Claude Code login (the UI runs `claude auth login`
  and shows its URL; you paste the code back) and its own `clodfarm run` process on the same queue, paced on that
  account's budget. The farm's first login can be done the same way.
- One password, no username: `FARM_UI_PASSWORD`, `clodfarm ui-passwd`, or a generated one printed once in the log.
  PBKDF2 hashes, HMAC-signed HttpOnly SameSite=Strict cookies, a CSRF header on every write, a login lockout and a
  strict CSP. Compose publishes the port on 127.0.0.1 only.
- Git changes are locked across processes too (several agents on one box share the repo).

## 0.1.2 (2026-09-25)

- Talk to the farm from your phone: every session, including the ones you open through Remote Control, now knows
  `clodfarm mission`, `status`, `pause` and `resume`, and answers requests like "set the mission to …" by running
  them.
- The device shows up in the Claude app under the farm's name instead of a random container id.

## 0.1.1 (2026-09-25)


- Pacing slows the farm but never stops it: at least `FARM_MIN_WORKERS` (1) agent keeps working unless a hard limit
  applies (5-hour ceiling, weekly target, rejection, paid overage). Found in the public end-to-end test, where a week
  that had just reset froze a fresh install at 0 agents.
- One source for the governor defaults (80% weekly, 85% per 5-hour window).
- The installer passes every `FARM_*` setting from your shell into the container, e.g.
  `FARM_VERIFY_CMD="pytest -q"`.

## 0.1.0 (2026-09-25)

First public release. Site: https://clod.farm

- `clodfarm run`: a supervisor with a Remote Control keeper and N headless Claude Code workers.
- A shared task queue (SQLite on one box, DynamoDB across boxes): priorities, leases, retries, parent and child
  tasks, resuming a parent in its own session.
- Budget governor on Claude Code's real `rate_limit_event` utilization: weekly pacing (80% target by default), a
  5-hour ceiling (85%), pause on rejection, no paid overage. API-key mode with a daily dollar cap.
- A planner that keeps the agents busy from `MISSION.md`.
- A git worktree per task. Sub-tasks branch from their parent, and top-level tasks rebase and fast-forward into main.
- A multi-arch Docker image (`ghcr.io/matank001/clodfarm`), a single-container compose file, and an AWS
  CloudFormation deploy with no inbound ports.
- **Zero-setup single box:** a built-in SQLite store (no database to run), a one-line installer, an optional `.env`,
  and `clodfarm mission`. DynamoDB only when one farm spans several boxes (`FARM_STORE=dynamodb`).
- **Several Claude accounts in one farm:** a per-seat budget, slots and spend, a shared queue, task branches shared
  through origin, and session affinity for resumed parents. `clodfarm budget` and `status` show every seat.
- Quality gate (`FARM_VERIFY_CMD`): check the rebased branch before it lands; resume the agent to fix failures.
- Timeouts resume the session instead of starting over; a circuit breaker pauses the farm after repeated failures.
- Notifications to ntfy, Slack or Discord (`FARM_NOTIFY_URL`); `FARM_EFFORT`.
- Usage-limit detection reads only structured events and error results, never the agent's own text.
- Login three ways: remote `clodfarm login`, a `claude setup-token` token, or an existing Linux profile.
