# What the Claudes know (and how to steer them)

Every Claude on the farm reads the **farm guide** (text in `clodfarm/prompts.py`): it is written into each Claude's
user-level `CLAUDE.md` (between `clodfarm:guide` markers), so the conversations you open through Remote Control know
the farm, and every sub-agent gets it appended to its system prompt plus a line naming its id, depth and worktree.

The guide teaches them to:
- **Know their budget:** `clodfarm agents` shows every Claude's 5-hour and 7-day budget left and how many more
  sub-agents it can start. The governor paces each account; they don't try to get around limits.
- **Save budget:** a sub-agent without `--on` runs on whichever Claude has room, so a Claude that is running low
  hands big jobs to sub-agents instead of doing them in its conversation.
- **Start sub-agents you can see:** `clodfarm spawn "<title>" --prompt "<self-contained instructions>" [--on NAME]`,
  then `clodfarm subagents` / `clodfarm result <id> [--wait]`. Inside a sub-agent, new sub-agents become its
  children; it ends its run and is resumed in the same session with their results. It never sleeps or polls.
- **Use Claude Code's Agent tool** only for quick look-ups (it isn't visible on the farm or paced).
- **Work with the other Claudes:** `clodfarm msg <name> "..."` (it appears in that Claude's next turn through a
  Claude Code hook; `clodfarm inbox` lists them), to hand off a mission, ask for a review or avoid collisions.
- **Schedule work:** `clodfarm schedule add "<title>" --prompt "..." --cron "0 9 * * 1-5" --tz <zone>` (or
  `--every 2h`, `--at "in 3h"`); `clodfarm schedule list` / `remove <id>`.
- **Commit on their branch** (sub-agents) and not push or merge. The farm does that.
- **End with a plain summary.** That is the sub-agent's result.
- **Stay safe:** never touch credentials; no messages outside the farm, payments, account creation or public posts
  unless the person they work for asks.

## Limits that keep a swarm sane

| Setting | Default | Why |
|---|---|---|
| `FARM_MAX_DEPTH` | 3 | sub-agent nesting |
| `FARM_MAX_QUEUE` | 25 | sub-agents can't start more once this many are waiting (you and your Claude can) |
| `FARM_MAX_ATTEMPTS` | 3 | per sub-agent, rate-limit retries excluded |
| `FARM_MAX_RESUMES` | 5 | a parent re-runs at most this often for late children |
| `FARM_TASK_TIMEOUT` | 5400 s | one agent run |

## Customising

- **Model:** `FARM_MODEL` applies to every agent.
- **Tools and MCP servers:** anything in the container's Claude Code config (`/home/farm/.claude/settings.json`,
  `claude mcp add ...` via `docker exec`) applies to all agents.
- **Project rules:** a `CLAUDE.md` in your work repo is read by every agent, like in any Claude Code project.
