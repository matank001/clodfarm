# Architecture

clodfarm is a small Python daemon (`clodfarm run`; the standard library plus boto3) that supervises Claude
Code processes and keeps its state in one store:
- **SQLite**, one file in the workspace volume, for a single box (the default);
- **a DynamoDB table** (`FARM_STORE=dynamodb`) when several boxes and accounts share one farm.

Both backends implement six primitives (`backends.py`), and all logic is written once on top of them (`store.py`).
Every update is an optimistic, versioned read-modify-write, so claims and counters are atomic on both.

```
container (user "farm", tini as PID 1)
└── clodfarm run                       supervisor.py
    ├── remote-control keeper          `claude remote-control --name $FARM_NAME --spawn worktree`, restarted with back-off
    ├── worker w0..wN-1                one thread each, never more than FARM_MAX_WORKERS
    │     └── claude -p --output-format stream-json --verbose --append-system-prompt <farm guide> ...
    └── housekeeping                   re-queues tasks whose lease expired, prints a status line every 10 min
```

## Modules

| File | Responsibility |
|---|---|
| `governor.py` | Pure function `decide(snapshot, policy, now) -> Decision(workers, reason, pause_until)`. No I/O. |
| `store.py` | Tasks, runs, per-seat budget, slots, control switches, heartbeats, events: every state change is a versioned conditional update. |
| `backends.py` | The two stores behind it: SQLite (WAL, safe across threads and `docker exec` processes) and DynamoDB. |
| `runner.py` | Starts one `claude -p` process, streams its JSON events, collects the result, usage and `rate_limit_event`s, and enforces a timeout. |
| `supervisor.py` | The worker loop, the planner trigger, resume logic, git integration, the Remote Control keeper. |
| `gitops.py` | One worktree and branch per task; rebase onto main plus fast-forward; conflicts become tasks. |
| `prompts.py` | The farm guide every agent gets, the planner prompt and the resume prompt. |
| `auth.py` | Login detection, the waiting banner, onboarding and trust flags, the guide in `CLAUDE.md`. |
| `cli.py` | The `clodfarm` command, shared by humans and agents. |

## Data model (one table, same shape in SQLite and DynamoDB)

| PK | SK | What |
|---|---|---|
| `TASK#<id>` | `META` | A task: title, prompt, status, priority, parent, children, depth, attempts, session id, worktree, result. `GSI1PK = STATUS#<status>` and `GSI1SK = <9-priority>#<created>` make "next queued task" one index query. |
| `TASK#<id>` | `RUN#<ts>` | One agent run: duration, turns, output tokens, list-price cost, utilization before and after. |
| `BUDGET` | `<seat>` | Newest `rate_limit_event` snapshot of that Claude account. The write is conditional on `observed_at` being newer. |
| `SLOT` | `<seat>#000..` | Per-seat concurrency slots with leases, shared by every box on that seat. |
| `SPEND` | `<seat>#<day>` | API-mode list-price spend per seat and day. |
| `CONTROL` | `GLOBAL` / `PLANNER` | The pause switch; planner single-flight and back-off. |
| `WORKER` | `<farm>/<worker>` | Heartbeats (TTL 1 day). |
| `EVENT#<day>` | `<ts>#<rand>` | Event log (TTL 30 days). |

Task IDs start with a timestamp, so they sort by creation time.

## Task lifecycle

```mermaid
stateDiagram-v2
  [*] --> queued: task add
  queued --> running: claim (conditional on status=queued) + lease
  running --> done: success, no open children
  running --> waiting: success, has open children
  waiting --> queued: last child finished (resume=true)
  running --> queued: success but children finished during the run (resume)
  running --> queued: failure, attempts left / rate limited / lease expired
  running --> failed: failure, no attempts left
  queued --> cancelled
  waiting --> cancelled
  failed --> queued: retry
```

**Resuming a parent:** a resumed parent runs `claude -p --resume <session_id>` in the same worktree, with a prompt
that lists every child's status and result. It keeps its whole conversation. If the session file is gone (a new
volume), it starts fresh with its original prompt plus the children's results.

**Why the parent ends its run instead of waiting:** a waiting agent would hold a slot, and with it the budget, while
doing nothing. Parents end their run and are re-queued when their children finish. Cycles are impossible because
depth is bounded by `FARM_MAX_DEPTH` (3).

## Git flow

- Each task gets `/workspace/.worktrees/<id>` on branch `farm/<id>`, cut from `main`. If the repo has an
  `origin`, `main` is pulled first.
- On success, anything left uncommitted is committed for the agent. The branch is rebased onto `main`, and `main`
  is fast-forwarded to it (under a lock, one merge at a time per box), then pushed if `origin` exists and
  `FARM_PUSH=1`.
- A rebase conflict aborts the rebase and queues a priority-8 "resolve merge conflict" task.
- Planner runs happen in the main checkout and are told not to change anything.
- Remote Control sessions spawned from the app use `--spawn worktree` too. The one Remote Control session
  pre-created in `/workspace/repo` sits on the main checkout, so commit there with care: the farm fast-forwards
  `main` underneath it.

Without a git repo in `/workspace/repo`, tasks run in `/workspace` with no isolation. The farm creates an empty repo
on first start, so that only happens if you remove it.

## Failure handling

| Failure | What happens |
|---|---|
| The agent process crashes | The attempt counts. The task is re-queued until `max_attempts` (3), then marked `failed` with the output. |
| The agent run times out | The session is resumed ("you hit the time limit; commit what's good and finish or split") up to `FARM_TIMEOUT_RESUMES` (2) times, keeping the work and the attempt. |
| `FARM_VERIFY_CMD` fails before landing | The branch is rebased onto main and the check runs in the worktree. On failure the agent is resumed with the output, up to `FARM_VERIFY_FIXES` (2) times. After that the task fails and main is untouched. |
| Many failures in a row | After `FARM_STALL_THRESHOLD` (5) failed runs in a row on any box, the circuit breaker pauses the whole farm and notifies you. `clodfarm resume` resets it. |
| The container or box dies mid-run | The lease (5 min, renewed every ~100 s) expires. Housekeeping on any box re-queues the task. The slot lease expires too. |
| DynamoDB is briefly unreachable | Worker threads log and retry after 30 s. The event log never raises. |
| Remote Control exits | Restarted after 10 s, with exponential back-off up to 30 min if it keeps failing fast. |
| Usage limit | See [budget.md](budget.md): the attempt is handed back and the whole account pauses until the reset. |
| Logged out (token revoked) | Runs fail with auth errors. `clodfarm doctor` shows it. Log in again with `clodfarm login`. |

## Scaling out

Run the same image on more boxes with the same `FARM_TABLE` (real DynamoDB) and the same `FARM_REPO_URL`:
- **Shared:** they share the queue, and task branches travel through origin.
- **Per seat:** each box works out its seat from its login. The budget, slots and spend are per seat, and the
  governor caps each seat's concurrency across all of that seat's boxes.
- **Per box:** `FARM_MAX_WORKERS` is per box.
- **Sessions:** a resumed task prefers the box holding its session for `FARM_RESUME_AFFINITY` seconds.

See [multi-seat.md](multi-seat.md).

## What clodfarm deliberately doesn't do

- No web UI. The Claude app (Remote Control), the CLI and `docker logs` cover it.
- No multi-account pooling, no API-key fallback and no limit evasion.
- No outbound messages, payments or posting. Agents are told not to unless your mission says so, and nothing in
  clodfarm itself does any of it.
