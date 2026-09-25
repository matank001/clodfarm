# What the agents know (and how to steer them)

Every headless agent gets the **farm guide** appended to its system prompt (`--append-system-prompt`, text in
`clodfarm/prompts.py`), plus a line naming its task id, depth and worktree. The same guide is written into the
user-level `CLAUDE.md` in the container (between `clodfarm:guide` markers), so sessions you open through Remote
Control know the farm too and can queue work for it.

The guide teaches them to:
- **Split big work:** `clodfarm task add "<title>" --prompt "<self-contained instructions>" --parent $FARM_TASK_ID`,
  then end the run. They're resumed in the same session with the results. They never sleep or poll.
- **Use built-in sub-agents** (Claude Code's Agent tool) for small parallel look-ups inside one run.
- **Commit on their branch** and not push or merge. The farm does that.
- **End with a plain summary.** That's what the parent task and the planner read.
- **Check `clodfarm budget`** before queueing a large batch. They leave pacing to the governor instead of
  rationing themselves.
- **Stay safe:** never touch credentials; no messages, payments, account creation or public posts unless the
  mission says so.

## MISSION.md

Put a `MISSION.md` at the root of the work repo (or at `/workspace/MISSION.md`). When the queue is empty and there's
budget, **one** planner run (single-flight across all boxes, at most once per `FARM_PLANNER_COOLDOWN`) reads:
- the mission;
- the last 15 finished and 5 failed tasks with their results;
- everything still open.

It then queues 1 to `max_queue/3` concrete tasks. If there's nothing useful to do, it says `IDLE:` and queues
nothing, and the planner backs off exponentially (up to `FARM_PLANNER_MAX_BACKOFF`, 6 h). Any new task you add
resets the cycle.

A good mission has:
- **the outcome**, not the steps ("a CLI that ..., with tests and docs");
- **the constraints** (language, style, what never to touch, what "done" means);
- **how to check the work** (test command, lint, a script);
- **when to stop** ("stop once X ships", or "keep improving Y").

See [examples/MISSION.md](../examples/MISSION.md).

## Limits that keep a swarm sane

| Setting | Default | Why |
|---|---|---|
| `FARM_MAX_DEPTH` | 3 | sub-task nesting |
| `FARM_MAX_QUEUE` | 25 | agents can't queue past this (humans can) |
| `FARM_MAX_ATTEMPTS` | 3 | per task, rate-limit retries excluded |
| `FARM_MAX_RESUMES` | 5 | a parent re-runs at most this often for late children |
| `FARM_TASK_TIMEOUT` | 5400 s | one agent run |

## Customising

- **Model:** `FARM_MODEL` applies to every agent.
- **Tools and MCP servers:** anything in the container's Claude Code config (`/home/farm/.claude/settings.json`,
  `claude mcp add ...` via `docker exec`) applies to all agents.
- **Project rules:** a `CLAUDE.md` in your work repo is read by every agent, like in any Claude Code project.
