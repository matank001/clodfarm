# Changelog

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
