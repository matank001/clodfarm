# Bots: other models on the farm

A **bot** is a farm member that runs another model: a free one on OpenRouter, your own through Ollama, or anything
behind an Anthropic-compatible gateway. It is still Claude Code, pointed at that provider with
`ANTHROPIC_BASE_URL`, so everything the farm does works the same: sub-agents in their own worktrees, resume,
messages, hooks, the browser tools. Only the model differs.

It uses **no Claude account's usage**, so it adds capacity when your subscriptions are the limit. It is also
**weaker than Claude**, so the farm keeps it on a short leash:

- **It takes only the sub-agents sent to it** (`clodfarm spawn ... --on <bot>`). A sub-agent without `--on` still
  goes to a Claude with budget. You can let a bot take any sub-agent when you add it.
- **Its own sub-agents stay on it**, so a bot never spends a Claude account's usage.
- **Nobody talks to it.** It has no Remote Control (that needs a Claude login): your Claudes hand it work.
- The guide tells your Claudes what a bot is good for: well-specified, low-risk jobs such as a first draft,
  boilerplate, a search or a summary. They check its result before relying on it.

## Add one

In the farm UI: **+ NEW CLAUDE → BOT: OTHER MODEL**. Pick the provider, name the model, paste the API key and click
**CHECK & ADD BOT**. The farm asks the model for one word first and keeps the bot only if it answers. The bot is on
the farm a few seconds later, wearing headphones.

From a shell (the key is read from stdin, or from an environment variable with `--key-env`):

```bash
docker exec -i clodfarm clodfarm bot add qwen --provider openrouter --model qwen/qwen3-coder:free <<< "$OPENROUTER_KEY"
docker exec -it clodfarm clodfarm bot add local --provider ollama --model qwen3-coder     # Enter: no key
```

`--workers N` lets it run up to 4 sub-agents at a time (default 1, right for free tiers). `--any` makes it take any
sub-agent. Release a bot in the farm UI like any Claude; its key goes with it.

| Provider | Address (default) | Key | Notes |
|---|---|---|---|
| OpenRouter | `https://openrouter.ai/api` | from openrouter.ai/keys | Free models end in `:free`. Free tiers allow a few requests a minute and a daily cap. |
| Ollama | `http://host.docker.internal:11434` | none | Ollama 0.14 or newer (it speaks Anthropic's API since then) on the machine running the container. On Linux, add `extra_hosts: ["host.docker.internal:host-gateway"]` to the compose service. |
| Anthropic-compatible | yours | if it needs one | Any endpoint that speaks Anthropic's Messages API, such as a LiteLLM gateway. The address is its base URL, without `/v1`. |

**Pick a model that can use tools.** Claude Code works through tool calls (reading files, running commands,
editing). A model that can't call tools reliably answers in prose and gets nothing done. Coding models such as Qwen3
Coder, and larger general models, do best.

## How it works

A bot is an agent like the ones you add with a Claude login: its own Claude config dir and its own `clodfarm run`,
started and kept running by the farm UI's process. Instead of a login, its environment points Claude Code at the
provider:

- `ANTHROPIC_BASE_URL` is the provider, and `ANTHROPIC_AUTH_TOKEN` its key (sent as `Authorization: Bearer`).
- `ANTHROPIC_MODEL` and every model Claude Code picks for itself (`ANTHROPIC_DEFAULT_*_MODEL`, the sub-agent model)
  are the bot's model, so nothing is sent to a Claude model by mistake.
- The container's own login (`CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`) is never passed to it.

It is paced like [API key mode](budget.md): there are no subscription windows to follow. When its provider
rate-limits it (a 429), its workers pause for 15 minutes; the other Claudes keep going. Claude Code prices every run
as if it were a Claude model, which is no bot's real cost, so a bot's spend isn't counted toward
`FARM_DAILY_BUDGET_USD` and `FARM_TASK_BUDGET_USD` doesn't cap its runs. Use the provider's own limits for that.

`clodfarm agents` and the farm UI mark it `BOT on <model> via <provider>`; its seat is `bot-<name>` in
`clodfarm budget`.

## Keep in mind

- **Your code goes to that provider.** A bot's sub-agent sends its prompt and the files it reads to the provider,
  under the provider's terms. Free tiers may log prompts or use them for training: read their policy before you send
  a bot work on private code.
- **The key** is kept in the bot's config dir (`bot.json`, readable by the farm's user only) and never shown again.
  Anyone who can run commands on the box as that user (every Claude, too) could read it, as with the farm's other
  secrets.
- A bot is not a way around Claude's limits: it never uses a Claude account at all.
