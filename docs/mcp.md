# Connect Claude Code on your computer (MCP)

The farm is a **remote MCP server**. Add it to Claude Code once, and the Claude on your laptop can:
- see the farm;
- hand work to it;
- talk to the farm's Claudes.

It does this without holding anyone's Claude login.

```bash
claude mcp add --transport http --scope user farm https://clod.farm/<team>/mcp     # or http://localhost:8080/mcp
```

Then run `/mcp` in Claude Code, pick **farm** and choose *Authenticate*. A browser page on the farm itself asks for:
- **being signed in to your Claude on the farm** in that browser (MY CLAUDE, with a code from "farm login");
- **a name for your computer**, for example `matan-laptop`;
- **what it may do:** see and work (the default), or only see.

Claude Code gets a token for this farm only. `clodfarm connect` on the farm prints the exact `claude mcp add` line.

## What your Claude Code can do

| Tool | Scope | |
|---|---|---|
| `farm_status` | read | the Claudes, each one's 5-hour and 7-day usage, room for sub-agents, what's running |
| `farm_budget` | read | every seat's usage windows and what the governor allows now |
| `farm_subagents` · `farm_result` | read | sub-agents; one sub-agent's prompt, result and runs (`wait_seconds` up to 45) |
| `farm_events` · `farm_sessions` · `farm_session` | read | the event log; every session and its conversation |
| `farm_inbox` · `farm_schedules` | read | messages the Claudes sent you; the schedules |
| `farm_spawn` | work | start a sub-agent: any Claude with budget runs it, or `on` picks one |
| `farm_msg` | work | message a Claude by name; it lands in its next turn |
| `farm_cancel` · `farm_retry` | work | stop or restart a sub-agent |
| `farm_schedule_add` · `farm_schedule_remove` | work | schedules (`cron` + `tz`, `every`, `at`) |

A connection can never:
- log Claudes in or out, release them, or pause the farm (those stay in the farm UI);
- read credentials.

Your computer is a **guest** on the farm, not a Claude, so it adds no budget and uses none. Its sub-agents run on the farm's Claudes, under the budget governor like any other. They show on the farm's own Claude's plot, marked `(for <name>)`. A farm Claude answers you with `clodfarm msg <name> "..."`, and you read the answer with `farm_inbox`.

## Put your computer's sessions on the farm (`clodfarm attach`)

The MCP connection lets your Claude Code *use* the farm. `clodfarm attach` also puts your computer's own Claude Code
sessions *on* it: each one shows under your Claude on the farm as a **LOCAL** session, with its conversation, next to
the farm's own, and messages the farm's Claudes send your computer arrive in the session itself.

```bash
pip install git+https://github.com/matank001/clodfarm
cd ~/code/my-repo && clodfarm attach https://clod.farm/<team>     # signs in once, in your browser
```

It signs in on the same consent page as `/mcp`, so be signed in to your Claude on the farm (MY CLAUDE) in that browser.
Then it adds one hook to Claude Code's user settings (`~/.claude/settings.json`, other hooks are kept) and a `/farm`
command.

**Which sessions go on the farm.** None by default. You choose, and the first rule that applies decides:

| | How | |
|---|---|---|
| 1 | `/farm` · `/farm off` · `/farm status` typed in a session | that session, from now on. The hook takes the command; it never reaches the model |
| 2 | `FARM=1 claude` · `FARM=0 claude` | that session |
| 3 | `clodfarm attach --all` | every session |
| 4 | `clodfarm attach` in a folder, or `--only DIR ...` | sessions started in those folders, or inside them |

`clodfarm attach --manual` connects nothing by itself (only rules 1 and 2), `clodfarm attach --status` shows what's
on, `clodfarm detach` stops a folder (`--only DIR`), and `clodfarm detach --all` removes the hooks and `/farm` and ends
the connection. Folder and `FARM=` changes apply to new sessions; `/farm` works in a running one.

**What is sent.** For a connected session, the hook sends what's new in its conversation each time a session starts,
at each prompt, when a turn ends and when it ends: your messages, Claude's replies, and each tool call and result in
short (as the farm keeps its own sessions). Thinking isn't sent. Secrets are scrubbed on your computer first (anything
shaped like a key, a token, an email address or a card number). A session that isn't connected sends nothing.
`/farm` sends the conversation so far; `/farm off` and then `/farm` again keeps what was said in between on your
computer.

**What it doesn't change.** The session still runs on your computer, on your own Claude account: the farm's budget
governor doesn't pace it, and it counts for no one's budget. A farm that is down or slow costs a session a few seconds
at most; what couldn't be sent waits in `~/.config/clodfarm` and goes with the next report.

**Messages.** The farm's Claudes reach your computer with `clodfarm msg <its name> "..."`, as before. In an attached
session the message arrives at your next prompt, or before Claude ends its turn. Unlike a Remote Control
conversation, an idle terminal session isn't woken for it.

Under the hood: the hook (`clodfarm hook --guest`) posts to the farm's `/mcp/hook` with the connection's token
(refreshed under a lock, since refresh tokens rotate). The farm records the session as kind `guest`, `runs_on` your
computer's name, under the Claude whose person approved the connection. A computer can only write its own sessions.

## How it's secured

- **OAuth 2.1** with the MCP authorization flow:
  - protected-resource metadata (RFC 9728) and authorization-server metadata (RFC 8414);
  - dynamic client registration (RFC 7591);
  - **PKCE S256 only**; the authorization response carries `iss`.
- **Redirect addresses** must be loopback `http` (`localhost`, `127.0.0.1`, `::1`) or `https`, registered up front, and matched exactly. The farm never redirects an error to an unregistered address.
- **The consent page** is the farm's own:
  - it is signed against tampering (the form is bound to the exact request for 15 minutes);
  - it runs under a strict CSP;
  - it shares the UI's login lockout (5 wrong passwords in 5 minutes).

  A connection's name can't be a farm Claude's name, so a guest can never read a Claude's messages.
- **Tokens:**
  - access tokens last an hour and are bound to this farm's `/mcp` URL (RFC 8707);
  - refresh tokens last 30 days and **rotate**: using an old refresh token again ends the whole connection, since that means it was copied;
  - all of them are stored only as SHA-256 hashes, in `/workspace/.farm/mcp-auth.json` (mode 600).
- **The MCP endpoint** refuses browser requests from other origins (DNS rebinding) and answers `401` with a `WWW-Authenticate` challenge that points clients to the metadata.

The farm's manager sees what's connected in **MANAGE → COMPUTERS (MCP)** and can **DISCONNECT** one, or turn MCP off for
the whole farm: every computer is then refused (and none can connect) until it's on again. From a shell,
`clodfarm connections` lists what's connected (name, scope, client, last use) and `clodfarm disconnect ID` ends one at once. Signing in and ending a connection are both in `clodfarm events` (`mcp.connected`, `mcp.disconnected`).

## Behind a proxy

Claude Code must reach the farm over HTTPS (or `localhost`). Behind a reverse proxy, set:
- `FARM_UI_BASE` for a path prefix;
- `FARM_PUBLIC_URL` to the URL people use, e.g. `https://clod.farm/team`, when the proxy rewrites `Host` (CloudFront does).

The farm answers the discovery documents both under its prefix and at the root, path-inserted as RFC 8414 and 9728 put them:
- `/.well-known/oauth-authorization-server/<prefix>`
- `/.well-known/oauth-protected-resource/<prefix>/mcp`

So route those root paths to the farm too. The proxy must pass the `Authorization` header through.
