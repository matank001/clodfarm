# What the Claudes know (and how to steer them)

Every Claude on the farm reads the **farm guide** (text in `clodfarm/prompts.py`): it is written into each Claude's
user-level `CLAUDE.md` (between `clodfarm:guide` markers), so the conversations you open through Remote Control know
the farm, and every sub-agent gets it appended to its system prompt plus a line naming its id, depth and worktree.

The guide teaches them to:
- **Know their budget:** `clodfarm agents` shows how much of every Claude's 5-hour and 7-day usage is used and how many more
  sub-agents it can start. The governor paces each account; they don't try to get around limits.
- **Save budget:** a sub-agent without `--on` runs on whichever Claude has room, so a Claude that is running low
  hands big jobs to sub-agents instead of doing them in its conversation.
- **Start sub-agents you can see:** `clodfarm spawn "<title>" --prompt "<self-contained instructions>" [--on NAME]`,
  then `clodfarm subagents` / `clodfarm result <id> [--wait]`. In a conversation (the Claude app), a Claude waits for
  them **in the background** (`clodfarm result <id> --wait` with Bash's run_in_background) and ends its turn, so you
  can keep talking to it while they work; Claude Code wakes it with the result. Ask it to change or stop a running
  sub-agent and it relays that at once (`clodfarm msg <id>`, `--urgent` to interrupt), as your instruction. Inside a sub-agent, new sub-agents become its
  children; it ends its run and is resumed in the same session with their results. It never sleeps or polls.
- **Use Claude Code's Agent tool** only for quick look-ups (it isn't visible on the farm or paced).
- **Work with the other Claudes:** hand off a mission, ask for a review or avoid collisions with a message (see
  [Messages](#messages) below).
- **Schedule work:** `clodfarm schedule add "<title>" --prompt "..." --cron "0 9 * * 1-5" --tz <zone>` (or
  `--every 2h`, `--at "in 3h"`); `clodfarm schedule list` / `remove <id>`.
- **Commit on their branch** (sub-agents) and not push or merge. The farm does that.
- **End with a plain summary.** That is the sub-agent's result.
- **Build apps on AWS** (only when the optional apps role is on): `aws --profile apps ...`, one CloudFormation stack
  per app, roles named `<prefix>-*` with the permissions boundary, within the monthly budget
  ([deploy-aws.md](deploy-aws.md#let-the-farm-build-apps-on-aws-optional)).
- **Stay safe:** never touch credentials; no messages outside the farm, payments, account creation or public posts
  unless the person they work for asks.

## Messages

Two ways, and the guide tells the Claudes when to use which:

| | Reaches | Delivered |
|---|---|---|
| Claude Code's `SendMessage` (find the session with `ListAgents`) | the live sessions on this box: every Claude's conversations and every sub-agent (`[clodfarm] <claude> · <title> · <id>`) | at its next tool call; an idle session starts a turn for it |
| `clodfarm msg <name or sub-agent id> "..."` | anyone, on any box, running or not (it waits in the store) | see below |

A `clodfarm msg` is handed over exactly once, by the first of:
- **With the prompt:** a sub-agent that isn't running gets it when it next starts.
- **After its next batch of tool calls:** every Claude's doorbell in the store is checked every `FARM_MAIL_POLL`
  seconds; new mail for its conversations or its running sub-agents sets a flag file, and a `PostToolBatch` hook
  hands the mail over (without a flag the hook doesn't even start Python).
- **Before a turn ends:** the `Stop` hook keeps the turn going with the mail (at most three times in a row).
- **Waking an idle conversation:** after each turn of a Remote Control conversation (the Claude app,
  claude.ai/code) an async hook waits up to 10 minutes and wakes it when mail arrives (Claude Code's `asyncRewake`).
- **At the next prompt**, as before.
- **`--urgent`** (a running sub-agent): sub-agents read stream-json on an open stdin (`FARM_LIVE_STDIN`), so the farm
  interrupts it (a running tool is cancelled) and hands the message over as its next turn.
- **`--wake`:** if nobody has read it after `FARM_MAIL_WAKE_AFTER` seconds, the farm starts a "mail" sub-agent on that
  Claude's account (one at a time), or resumes the finished sub-agent it was for, in its own session. A run started by
  a message counts a hop: after `FARM_MAIL_MAX_HOPS` hops, or `FARM_MAIL_WAKES_PER_HOUR` wakes per recipient,
  messages no longer wake anyone, so two Claudes can't keep each other busy.

Every message has an id and a reply address (a sub-agent's is its task id), and shows up in the event log.
`SendMessage` needs the sessions to see each other: every Claude added in the farm UI lists its sessions in the farm's
own Claude's list (`FARM_SHARE_SESSIONS`), and each Claude takes messages from them without holding them for approval
(`crossSessionInbound: accept`, unless you set it). Messages from another Claude are never the person's approval.

## Limits that keep a swarm sane

| Setting | Default | Why |
|---|---|---|
| `FARM_MAX_DEPTH` | 3 | sub-agent nesting |
| `FARM_MAX_QUEUE` | 25 | sub-agents can't start more once this many are waiting (you and your Claude can) |
| `FARM_MAX_ATTEMPTS` | 3 | per sub-agent, rate-limit retries excluded |
| `FARM_MAX_RESUMES` | 5 | a parent re-runs at most this often for late children |
| `FARM_TASK_TIMEOUT` | 5400 s | one agent run |
| `FARM_MAIL_MAX_HOPS` | 3 | message-triggered runs in a row before messages stop waking anyone |
| `FARM_MAIL_WAKES_PER_HOUR` | 6 | wakes per recipient and hour |

## Customising

- **Model:** `FARM_MODEL` applies to every agent, and is each Claude's default model for the sessions you open
  from the Claude app. An alias (`opus`) follows the newest model of that family.
- **Claude Code version:** the image ships the newest release (the `latest` channel) and the farm updates it every
  hour (`FARM_CLAUDE_UPDATE`, 0 = never). Sub-agents run on a new version at once; each Claude's Remote Control
  restarts on it once no one has talked to that Claude for 15 minutes.
- **Tools and MCP servers:** anything in the container's Claude Code config (`/home/farm/.claude/settings.json`,
  `claude mcp add ...` via `docker exec`) applies to all agents.
- **Project rules:** a `CLAUDE.md` in your work repo is read by every agent, like in any Claude Code project.
