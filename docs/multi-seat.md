# One farm, several boxes, several Claude accounts

A single box keeps its farm in a SQLite file. A **farm spread over several boxes** is one DynamoDB table
(`FARM_STORE=dynamodb`). Every box (container) pointed at the same table shares:
- **one farm:** any box whose account has budget can run any sub-agent (unless it is pinned with `--on`), including the sub-agents of a parent that runs elsewhere;
- **one git origin:** task branches travel through it, so work started on one box can be merged on another.

The **budget is per seat.** A seat is the Claude account a box is logged in to. Each box works out its seat from its
own login (for example `matan-3f2a`; the full email is never stored). Usage snapshots, concurrency slots and API
spend are all kept per seat, and the governor paces every seat against **its own** real 5-hour and weekly
utilization. So when one person's account hits a limit or gets close to its target, only that seat's boxes pause.
The others keep running the farm's sub-agents.

```
            ┌──────────── DynamoDB table (the farm) ────────────┐
            │  sub-agents · events · per-seat budget + slots    │
            └──────┬───────────────────┬───────────────────┬────┘
                   │                   │                   │
   box A (seat matan-3f2a)   box B (seat matan-3f2a)   box C (seat gil-9c1d)
   Matan's login             Matan's login             Gil's own login
                   └──────── git origin (shared repo) ─────┘
```

## Who can join, and the terms

- Every seat must be **its owner's own login.** The farm never copies or shares credentials, and each person logs
  in on their own box with `clodfarm login`.
- For people working together, use **Claude Team or Enterprise seats in one organisation**, each member on their
  own seat. That's what those plans are for.
- Don't combine personal Pro/Max subscriptions to get more capacity for one person's work, and don't run a
  subscription-backed farm as a service for others. Anthropic's consumer terms say plan limits assume ordinary,
  individual use and forbid reselling or intermediating usage. Use an API key for that.
- The governor paces each seat below its own limits (80% weekly and 85% per 5-hour window by default), so each
  person keeps room for their own Claude.

## Set it up

**1. The first box creates the farm.**
```bash
deploy/aws/deploy.sh up --workspace-repo git@github.com:your-org/your-repo.git   # stack "clodfarm", table "clodfarm"
deploy/aws/deploy.sh login
```
A multi-box farm needs a **shared git origin** that every box can push to (`FARM_REPO_URL` plus a deploy key or
token). Without one, each box has its own local repo, so run one box only.

**2. More boxes join the same table**, on your seat or on a teammate's:
```bash
STACK=farm-gil deploy/aws/deploy.sh up --table clodfarm --workspace-repo git@github.com:your-org/your-repo.git
STACK=farm-gil deploy/aws/deploy.sh login     # Gil runs this and logs in to HIS account
```
Any Docker host works too. Set these in `.env`:
- `FARM_STORE=dynamodb`
- `FARM_TABLE=clodfarm`
- `AWS_REGION=...`
- `FARM_REPO_URL=...`

Then give the box AWS credentials for that table, and run:
`docker compose up -d && docker exec -it clodfarm clodfarm login`.

**3. See every seat:**
```
$ clodfarm budget
BUDGET per Claude account (seat), from Claude Code's own rate-limit reports
SEAT gil-9c1d  boxes: farm-gil@b7e1
  5-hour    22% used   limit for agents 85%   resets Fri 18:00Z (in 3.1h)
  7-day     31% used   limit for agents 80%   resets Mon 07:00Z (in 3.2d)
  governor: 3/3 agents allowed now, 2 running: within budget: full speed
SEAT matan-3f2a  boxes: clodfarm@a1c2, farm-2@c3d4
  5-hour    86% used   limit for agents 85%   resets Fri 16:40Z (in 1.7h)
  governor: 0/3 agents allowed now, 0 running: 5-hour window at 86% (ceiling 85%)
```
*(Illustrative output.)*

## How work moves between boxes

- A task's branch is pushed to origin whenever it stays open: a parent waiting for sub-agents, or a finished
  sub-agent waiting for its parent.
  - A sub-agent on any box branches from its parent's pushed branch.
  - When the parent resumes, it fetches its children's branches and merges them.
  - When a top-level task lands on main, its whole tree of task branches is deleted from origin.
- **Sessions stay home.** A parent's conversation lives on the box it ran on. For `FARM_RESUME_AFFINITY` seconds
  (default 600) only that box may resume it. After that any box may, starting a fresh session with the parent's
  prompt and its children's results, so a dead box never blocks work.
- Seats can have different settings. Each box applies its own `.env` (target, ceiling, model, workers) to its own
  seat.

## Settings

| Variable | Default | |
|---|---|---|
| `FARM_TABLE` | `clodfarm` | same value on every box of one farm |
| `FARM_REPO_URL` | *(empty)* | the shared origin; required for more than one box |
| `FARM_SEAT` | *(from the login)* | override the seat name, e.g. to label boxes |
| `FARM_RESUME_AFFINITY` | `600` | seconds a resumed task waits for the box holding its session |
