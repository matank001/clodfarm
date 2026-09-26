# Changelog

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
- **Every Claude session and its whole conversation are in the farm's store.** A Claude Code hook (`clodfarm hook`,
  installed in each Claude's settings) runs on session start, each prompt, each reply and session end: it registers
  the session (which Claude, conversation or sub-agent, its task, its Remote Control session) and copies the new
  transcript turns (your messages, Claude's replies, tool calls and results in short; thinking is left out). Read
  them with `clodfarm sessions` / `clodfarm session ID`, or on each Claude's card in the farm UI. Messages from other
  Claudes are delivered at the next real prompt (never used up by a session that gets none).
- **Every farm session is marked `[clodfarm]`** in the Claude app and claude.ai/code: the Remote Control session
  (`[clodfarm] <name>`), the sessions you open from it, and each sub-agent (`[clodfarm] <name> · <job>`).
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
