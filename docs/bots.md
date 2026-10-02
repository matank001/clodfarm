# Bots: other models on the farm

A **bot** is a farm member that runs another model: GPT on OpenAI, Grok on xAI, Gemini, an open model on Groq, a
free one on OpenRouter, your own through Ollama, or anything behind an Anthropic- or OpenAI-compatible gateway. It is
still Claude Code, pointed at that provider with `ANTHROPIC_BASE_URL` (through the bot's own [relay](#the-relay) when
the provider speaks OpenAI's API), so everything the farm does works the same: sub-agents in their own worktrees,
resume, messages, hooks, the browser tools. Only the model differs.

It uses **no Claude account's usage**, so it adds capacity when your subscriptions are the limit, and it lets the
farm use another lab's model where that model is the better fit. The farm keeps it on a short leash:

- **It takes only the sub-agents sent to it** (`clodfarm spawn ... --on <bot>`). A sub-agent without `--on` still
  goes to a Claude with budget. You can let a bot take any sub-agent when you add it.
- **Its own sub-agents stay on it**, so a bot never spends a Claude account's usage, unless it sends one to a
  Claude with `--on` (which the planner does: see [the planner on a bot](planner.md#the-planner-on-another-model)).
- **Nobody talks to it.** It has no Remote Control (that needs a Claude login): your Claudes hand it work.
- The guide tells your Claudes what a bot is good for: well-specified, low-risk jobs such as a first draft,
  boilerplate, a search or a summary. They check its result before relying on it.

## Add one

In the farm UI: **+ ADD AGENT → ADD AGENT WITH API KEY**. Pick the provider and paste its API key; that's all it
needs. The model is optional (empty: the provider's latest, e.g. `gpt-6.1-sol`, `grok-4.7`, `gemini-3.1-pro-preview`),
and so is another endpoint (tick EDIT ENDPOINT). Click **CHECK & ADD AGENT**. The farm asks the model for one word
first and keeps the bot only if it answers. The bot is on the farm a few seconds later, wearing headphones.

From a shell (the key is read from stdin, or from an environment variable with `--key-env`):

```bash
docker exec -i clodfarm clodfarm bot add gpt --provider openai <<< "$OPENAI_API_KEY"          # its latest model
docker exec -i clodfarm clodfarm bot add grok --provider xai --model grok-4.7 <<< "$XAI_API_KEY"
docker exec -i clodfarm clodfarm bot add gemini --provider gemini --model gemini-3.1-pro-preview --effort high <<< "$GEMINI_API_KEY"
docker exec -i clodfarm clodfarm bot add qwen --provider openrouter --model qwen/qwen3-coder:free <<< "$OPENROUTER_KEY"
docker exec -it clodfarm clodfarm bot add local --provider ollama --model qwen3-coder     # Enter: no key
```

`--workers N` lets it run up to 4 sub-agents at a time (default 1, right for free tiers). `--any` makes it take any
sub-agent. Release a bot in the farm UI like any Claude; its key goes with it.

For a bot on the relay (OpenAI, xAI, Gemini, Groq, OpenAI-compatible):

- `--effort minimal|low|medium|high` sets the model's reasoning effort, for a model that has one.
- `--max-out N` caps the output tokens of each answer, for a model whose limit is below what Claude Code asks for.
- `--price-in`, `--price-out` and `--price-cached` set its list price in USD per million tokens. Without them the farm
  uses the prices it knows (`clodfarm/prices.py`), and `bot add` says when it knows none (its spend then shows as $0).
- `--daily-usd N` stops starting its sub-agents once it spent that much today, at list price.

| Provider | Address (default) | Key | Notes |
|---|---|---|---|
| OpenAI | `https://api.openai.com/v1` | from platform.openai.com/api-keys | Through the relay, on OpenAI's Responses API: the newest models take tools with reasoning only there. Their reasoning summaries show as thinking. |
| xAI | `https://api.x.ai/v1` | from console.x.ai | Through the relay. Grok's reasoning shows as thinking. |
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | from aistudio.google.com/apikey | Through the relay, on Google's OpenAI-compatible endpoint. Its thoughts show as thinking; its thought signatures go back with each tool call. |
| Groq | `https://api.groq.com/openai/v1` | from console.groq.com/keys | Through the relay. Open models (gpt-oss, Llama, Kimi) at high speed. |
| OpenAI-compatible | yours, with `/v1` | if it needs one | Through the relay: vLLM, LM Studio, a gateway. |
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
rate-limits it (a 429), its workers pause for 15 minutes; the other Claudes keep going.

**What it costs.** Claude Code prices every run as if it were a Claude model, which is no bot's real cost. A bot on
the relay is counted at its own model's list price instead (the tokens the provider reported, at the price it was
given or the one `prices.py` knows), in its seat's daily spend, so `--daily-usd` holds it and the planner sees what
each bot spent. A bot on an Anthropic-compatible provider isn't counted: use the provider's own limits for that.

## The relay

Claude Code only speaks Anthropic's Messages API. A bot whose provider speaks OpenAI's Chat Completions API gets its
own **relay**: a small server on `127.0.0.1` that its `clodfarm run` starts (detached, like a sub-agent's run, so a
new release adopts the runs that use it), and that Claude Code talks to as if it were Anthropic's API.

- Text, images, tool calls and tool results are translated both ways, streamed or not. Tool names longer than
  OpenAI allows, and Anthropic's own server tools, are handled.
- The model's reasoning (`reasoning_content`, `reasoning`, Gemini's thoughts) comes back as Claude Code's
  `thinking`, so the farm sees it think.
- What a provider needs to see again with a tool call (Gemini's thought signatures) rides in the tool call's id,
  which Claude Code sends back unchanged.
- A 429 stays a 429 (the bot pauses), a full context becomes "prompt is too long" (Claude Code compacts and goes on),
  anything else an Anthropic-shaped error.
- The provider's key stays with the relay, read from the bot's `bot.json`. Claude Code gets the relay's address and
  a token of its own.

When it's added, a bot on the relay is checked through the same translation, with a tool on offer, so a provider
that can't take Claude Code's tools fails there and not on its first sub-agent. Its log is
`.farm/agents/relay-<bot>.log`.

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
