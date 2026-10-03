# Changelog

## 1.11.0 (2026-10-03)

- **The live feed.** The farm keeps what its sub-agents think, say and do as they do it, for a day: thoughts (Claude's
  thinking, other models' reasoning), what they say, each tool they call, and messages between Claudes.
  `GET /api/live` reads it, with every seat's spend. It's for the farm's people, and for anyone when the farm
  **broadcasts** (`FARM_UI_BROADCAST=1`, or the manager's switch). Everything in it is scrubbed first: the farm's own
  secrets, and anything that looks like a key, a token, an email address or a card number. See docs/ui.md.
- **MCP servers from the farm's host.** `FARM_REMOTE_MCP` gives every Claude remote MCP servers (`mcp__<name>__*`)
  with their guide text. See docs/connectors.md.
- **Farm-style pickers.** Every menu on the farm's pages (the farm, TASKS, the browser) opens a pixel-art list
  instead of the system's, with each provider's mark beside it.
- **A shorter agent form.** The provider and its API key come first. The model is optional: with none named, the
  agent runs on the provider's latest (gpt-6.1-sol, grok-4.7, gemini-3.1-pro-preview...), in the UI, on an invite and
  in `clodfarm bot add`.

## 1.10.0 (2026-10-02)

- **+ ADD AGENT.** The dock's + NEW CLAUDE / + INVITE A CLAUDE is now + ADD AGENT, with the Claude, OpenAI and Grok
  marks. It opens two big choices: **ADD CLAUDE SUBSCRIPTION** (a Claude account logs in) and **ADD AGENT WITH API
  KEY** (Claude Code on GPT, Grok, Gemini, Groq or another model, paid by its key). The manager can invite someone
  from there too.
- **Each provider's endpoint, shown.** The agent form shows the provider's mark and its endpoint, each checked
  against its provider. EDIT ENDPOINT changes it, and a gateway of yours asks for one.
- **Generic invites.** The person you invite picks **MY CLAUDE SUBSCRIPTION** (it logs in) or **MY AGENT ON AN API
  KEY** (checked with its model before it's kept). Their agent joins the farm as theirs.
- **Sign in with a username and password**, next to a code from your Claude. Pick one when you add your agent
  (optional with a Claude; a must with an API key, which has no Claude to sign you in), or in SETTINGS → SIGN IN WITH
  A USERNAME. Passwords are kept as salted PBKDF2 hashes. Five wrong tries lock an address out for five minutes.
- The title screen (an invite, the sign-in) scrolls on a phone or a small window. The ticker no longer calls a bot
  added from the farm UI "a new egg".

## 1.9.0 (2026-10-02)

- **Bots on OpenAI, xAI (Grok), Gemini and Groq.** Add one from + NEW CLAUDE → BOT, or `clodfarm bot add gpt
  --provider openai --model gpt-6.1-sol` (also `xai`, `gemini`, `groq`, `openai-compatible`). Claude Code only speaks
  Anthropic's API, so each such bot gets its own relay: a small server on 127.0.0.1 that its `clodfarm run` starts,
  which translates Claude Code's calls to OpenAI's Responses API (OpenAI) or Chat Completions (the others), streamed
  or not. Text, images, tool calls and their results go both ways. The model's reasoning (OpenAI's reasoning
  summaries, Grok's reasoning, Gemini's thoughts) shows as thinking. What a model must see again on the next turn
  (OpenAI's encrypted reasoning, Gemini's thought signatures) goes back with it. A 429 still pauses the bot, and a
  full context makes Claude Code compact. The provider's key stays with the relay. Adding a bot checks it through
  the same translation, with a tool on offer. Tested with real Claude Code on gpt-6.1-sol, grok-4.7 and
  gemini-3.1-pro-preview.
- **What a bot costs.** A bot on the relay is counted at its model's list price: the price you give it
  (`--price-in/--price-out/--price-cached`, or the form's $/M fields), else the ones the farm knows. `--daily-usd`
  stops starting its sub-agents once it spent that much today. `clodfarm budget` and the planner show each bot's
  spend. `--effort` sets a model's reasoning effort, and `--max-out` caps its output.
- **The planner on any model.** MANAGE → THE PLANNER → RUNS ON (or `clodfarm planner host <bot>`) can pick a bot the
  manager added: the planner thinks on GPT, Grok or Gemini, sends the coding to the Claudes with `--on <claude>`,
  and keeps research and writing on the bots. It sees every bot's model and spend. See docs/planner.md.

## 1.8.1 (2026-10-01)

- Whiteboards work on Python 3.10 and 3.11 (1.8.0 needed 3.12; its image was never published).

## 1.8.0 (2026-10-01)

- **Whiteboards.** Every Claude has one (WHITEBOARD on the farm, or W; `/whiteboard/<claude>`): you draw and write
  on it, your Claude draws on it with `clodfarm board`, and both see the other's changes within a second. Everyone on
  the farm sees and draws on every board. A full editor: pen, arrows that join shapes and follow them, boxes, ovals,
  decisions, databases, hexagons, clouds and more with labels inside, text, sticky notes, frames, images (paste or
  drop), select, move, resize, copy, layers, undo, PNG. `clodfarm board diagram` lays out a whole Mermaid flowchart
  (or JSON nodes and edges) with nested groups, so a Claude draws a complex architecture in one command. It looks
  and works like Excalidraw: hand-drawn shapes and hachure fills (rough.js), Virgil's handwriting, an ink pen
  (perfect-freehand), floating tools with Excalidraw's keys, a style panel with its palette and sloppiness, and a
  welcome screen. See docs/whiteboard.md.
- **A REFRESH button on every dashboard.** It runs the dashboard's code now (`--run CMD --every manual` makes code
  that runs only then), or, for a dashboard without code, starts a sub-agent of the Claude that keeps it to collect
  the data and push it again (`--agent "how to get the numbers"`, or `refresh` on `farm_dashboard_push`, tells it
  how). The page shows it working and the new numbers the moment they land; a refresh that brings no data says why.
  The manager and the person of the Claude that keeps it start sub-agents; anyone on the farm runs a dashboard's code.

## 1.7.1 (2026-09-30)

- **MANAGE → COMPUTERS (MCP).** The farm's manager sees the computers connected to the farm over MCP (Claude Code on
  someone's laptop) and can DISCONNECT one, or turn MCP off for the whole farm: every computer is then refused and none
  can connect, until it's on again (the connections come back). The Blender connector is separate and keeps working.

## 1.7.0 (2026-09-30)

- **A Blender connector.** The farm manager connects a Blender MCP server that runs on another machine (CONNECTORS →
  BLENDER, or `clodfarm blender connect https://host/mcp` with its token on stdin), and every Claude gets its tools
  as `mcp__blender__*`: scenes, game assets, materials, animation, renders, import and export, Python. The farm checks
  the server answers before saving it; only the manager sees its URL and the token's last 4 characters. A person can
  turn it off for their Claude (SETTINGS → RULES, *Blender (3D)*). See docs/connectors.md.

## 1.6.2 (2026-09-30)

- The sign-in link and the code your Claude gives you ("farm login") each work once, on their own: tapping the link
  (in the Claude app's own browser, or a link preview opening it) no longer uses up the code you type on the farm's page.

## 1.6.1 (2026-09-29)

- The sign-in screen says where your Claude is: open the Claude app (or claude.ai/code), go to Code, open the session
  `[clodfarm] <farm>`, and send it "farm login".

## 1.6.0 (2026-09-29)

- **No passwords at all.** A private farm is seen by the people of its Claudes (and its manager), who sign in with
  their Claude; anyone else needs an invite. The viewer password is gone from MANAGE → PRIVACY, from the sign-in
  screen and from `clodfarm farm private` (its `--password` is ignored). A farm that had one stays private. Someone
  who only watched with that password now needs a Claude of their own on the farm (an invite), or the farm made
  public, where visitors see the tokens and the Claudes at work and nothing else.

## 1.5.2 (2026-09-29)

- **You sign in with your Claude.** The sign-in screen is one thing: SIGN IN WITH YOUR CLAUDE (tell your Claude
  "farm login" in the Claude app; it sends you a link, or a code to type there), or open an invite link. A private
  farm with a viewer password still takes the password to watch. `FARM_UI_SSO_URL` and the host's SIGN IN tab are
  gone: a host's one-time `/sso` link (FARM_UI_SSO_KEY) is only the way in right after paying.

## 1.5.1 (2026-09-29)

- **Your farm's own Claude gets the onboarding too:** LOG IN YOUR CLAUDE on a new farm first asks its name, look and
  rules, like any hatch, then logs it in.
- **More Claudes come by invite:** once the farm has its Claude, the big button is + INVITE A CLAUDE (a one-time link).
  Hatching one directly (a bot, or another account of yours) is one tap away in that dialog.
- A farm at its host's limit says so plainly: "this farm has room for 5 Claudes, and they're all here" (no talk of
  plans). An invite to a full farm shows that instead of a login button, and the manager's INVITE A CLAUDE says whether
  there's room left now.

## 1.5.0 (2026-09-29)

- **Invite a Claude** ([docs/people.md](docs/people.md#inviting-someone)). MANAGE → INVITE A CLAUDE, or
  `clodfarm invite`, makes a link for one person: it opens the farm on one button, LOG IN WITH YOUR CLAUDE, and their
  own Claude joins the farm. It works once, for 7 days, even on a private farm or with hatching closed, and it's spent
  at the login, not by a chat app's preview.
- **A public farm's visitor just watches:** the tokens burning and the Claudes at work, and the key to sign in. No
  dock, no chips, no textbox.

## 1.4.0 (2026-09-29)

- **Farms hosted for someone else** ([docs/people.md](docs/people.md#a-farm-hosted-for-someone)). With
  `FARM_UI_SSO_KEY`, the farm accepts a one-time `/sso` link signed by its host, which signs the device in as the
  person of the manager Claude (like a pairing link). `FARM_UI_SSO_URL` adds a SIGN IN button to the sign-in screen
  that goes to the host's account page. `FARM_UI_PRIVATE=1` keeps the farm private whatever its settings say.
  `FARM_MAX_CLAUDES` caps the Claudes and bots the farm adds, for everyone including the manager, from the UI, the
  CLI and MCP alike.
- **A new farm points at its first step.** Until a Claude is logged in, the big button bounces a START HERE
  arrow, pulses and wobbles its egg. For the manager of a farm whose own Claude is still an egg, it reads LOG IN
  YOUR CLAUDE, and so do its dialog and the welcome line. It greys out when the host's plan has no room.
- **Google Ads without a developer token.** An OAuth client and a refresh token are enough now (Google checks the
  Cloud project's API access level); the developer token is sent only when there is one. The API versions tried are
  v25, v24 and v23.

## 1.3.1 (2026-09-29)

- `clodfarm gads query` takes the query first, then `--customer ID`, so it works on Python 3.10 and 3.11 too (their
  argparse can't take a second argument after an option); the Claudes' guide and the docs say so.

## 1.3.0 (2026-09-29)

- **Connectors, and Stripe for every Claude** ([docs/connectors.md](docs/connectors.md)). The SLACK button becomes
  CONNECTORS: a menu with Slack, Stripe and Google Ads and how each is doing. The farm manager connects the farm's
  Stripe once (a restricted key is best; the farm checks it with Stripe and never shows it again), and every Claude
  gets Stripe's own MCP tools (`mcp__stripe__*`). Their guide says to move real money only when their person asks; a
  person can turn Stripe off for their Claude in its SETTINGS. From a shell: `clodfarm stripe [connect|disconnect]`.
- **Google Ads for every Claude.** CONNECTORS → GOOGLE ADS (or `clodfarm gads connect`) takes a developer token, an
  OAuth client and a refresh token (and a manager account's ID). The farm checks them with Google and lists the ad
  accounts they reach. Every Claude runs reports and gets tokens for changes with
  `clodfarm gads accounts | query | token`, and `clodfarm gads dashboard --customer ID` is a ready live dashboard
  (spend, clicks, conversions, CPA, per day and per campaign).
- **Fixed:** a Claude's SETTINGS → RULES showed "[object HTMLLabelElement]" instead of its switches. On a phone the
  dock's MANAGE reads ADMIN, so it fits.
- **README:** the demo, the architecture picture, the social preview and three new screenshots show the new farm.

## 1.2.1 (2026-09-29)

- **The Claudes walk round the farm again.** Free ones stroll the meadow, and so do the ones not running right now;
  the fenced yard by the barn is where Claudes nap while their budget is paced. Busy ones walk round their plot now
  and then to look at the crops, then go back to their laptop.

## 1.2.0 (2026-09-29)

- **The farm, redrawn, in the same theme.** It draws at your screen's real resolution (sharp on phones and Retina,
  nothing shimmers when you pan); every character is redrawn with twice the detail (the Claudes, hats, extras, minis,
  eggs, the scarecrow, crops) and new moves (walking, blinking, typing, napping, a cheer when work lands); the field
  sits fenced by the barn, with paths and things growing around it.
- **A real look picker** when you hatch a Claude or change its look in SETTINGS: a big live preview, hats and extras
  as tiles, colour swatches for its hat, band and body, SURPRISE ME; keyboard and touch friendly, a bottom sheet on a
  phone. SETTINGS is laid out in cards with its SAVE always in reach.
- **Your Claude, one tap away:** tapping YOUR CLAUDE (top left) opens its card, with ✎ CUSTOMIZE (straight to its
  look) and ⚙ SETTINGS at the top, next to its name.
- **A dock that explains itself:** the buttons at the bottom are labelled and grouped (YOUR CLAUDE, THE FARM, MANAGE,
  + NEW CLAUDE), their signs say what each does, and HELP (or ?) explains every button and everything on the farm.
- **Pasting into the farm's browser works:** ⌘/Ctrl+V types your clipboard where the cursor is, in any language. It
  used to go through the VNC clipboard, which x11vnc doesn't hand to the browser's display, so it pasted nothing.
  A TYPE box under the screen sends any text; on a phone, ⌨ KEYBOARD opens your keyboard and types into it.
- **The browser page shows only your own Claude's profiles**, the manager's too (a profile moves to another Claude
  with `clodfarm browser assign`).

## 1.1.0 (2026-09-29)

- **No admin password: the farm's manager is a Claude's person.** The person of a manager Claude runs the farm: at
  first the farm's own (first) Claude, e.g. matan on the jestr farm. They sign in to it like anyone ("farm login" in
  the Claude app, or MY CLAUDE with its code). In the manager panel, WHO RUNS THE FARM makes another Claude a manager
  too or hands the role over (a farm always keeps one); from the box's shell, `clodfarm farm manager [set|add|remove]
  <claude>`. `FARM_UI_PASSWORD`, `clodfarm ui-passwd` and `manager-passwd` are gone; a private farm keeps its viewer
  password. Connecting Claude Code over MCP now asks you to be signed in to your Claude on the farm.
- **NEW SESSION in the Claude app works again.** Since 1.0.0 Remote Control ran with its stdin a closed pipe, and the
  app's new sessions never started (the one it opened at start did). It gets /dev/null again, as before 1.0; one
  started the old way is restarted once none of its conversations has been active for 15 minutes.
- **`clodfarm browser` shows a Claude its own profiles** (the farm's own Claude also the ones nobody owns); a person
  at the box's shell still sees every profile. Found by a Claude on the jestr farm checking itself after the upgrade.
- A process mid-exec (a wrapper script execing the real program) is no longer taken for gone: its command line is
  empty for an instant on Linux.

## 1.0.2 (2026-09-29)

- **Dashboards and your Claude's browser on your phone.** Their buttons were desktop-only; they now show on a phone
  once you're signed in to your Claude (the toolbar's buttons get a little smaller to fit).
- **A Chromium already running for a profile is adopted, never doubled:** before starting one, the farm looks for a
  process with that profile and takes it over. Every process the farm starts is recognised by its arguments, not its
  program's path (a wrapper, or macOS's framework Python, execs into another one).
- **A new image wins over an older in-place release:** `deploy.sh roll` onto a newer image is no longer hidden behind
  a release that an earlier `clodfarm upgrade` left in the volume.
- **A Claude that is still waiting for its login hands over too** on `clodfarm upgrade` (it used to stop, and be
  started again), and `upgrade` no longer waits for it to be ready.
- The farm UI on macOS: requests taken from the shared socket are answered normally again.

## 1.0.1 (2026-09-29)

- **One Chromium per browser profile again.** In the image, `/usr/bin/chromium` is Debian's script that execs the
  real Chromium with its own flags before the farm's, so 1.0.0 never recognised the Chromium it had started: it
  started another at every check (3 a minute per profile) and could stop none of them, until the box ran out of
  memory. A Chromium is now known by its profile (`--user-data-dir`), kept in its pid file.
- **A new manager password in the environment signs the old manager sessions out.**
- **The UI's port is one socket shared by every UI process** on Linux: nothing is dropped during a roll.

## 1.0.0 (2026-09-29)

Your own Claude on a shared farm, missions that wait for your OK, a planner that works toward a goal all the time, a
farm manager, a farm that stays easy to watch with a hundred Claudes and a crowd, and new code without stopping a
single agent.

- **Upgrade without stopping the agents** ([docs/upgrades.md](docs/upgrades.md)). `clodfarm upgrade` (or
  `deploy/aws/deploy.sh upgrade`, or `install.sh | sh -s upgrade`) installs a new clodfarm into the workspace volume
  and hands over to it: every sub-agent keeps running mid-run, the phone conversations (Remote Control) too, and so
  do the Claudes added in the UI and the farm's browser. It ends by showing the agents are the same processes.
  - Every `claude` process runs under a small detached shim that keeps its stdin, output and exit code in files; the
    farm daemon reads them, and a new release (or the daemon after a crash) adopts the run and lands it as usual.
  - The farm UI is its own process, kept by the daemon, and is rolled with no downtime (the new one binds next to the
    old one). A crowd watching the farm talks only to the UI process.
  - A release that keeps crashing at start is taken back by itself; `clodfarm upgrade --rollback` does it by hand.
  - A new image drains the box instead: `clodfarm drain --exit` (or `deploy.sh roll`) stops taking work, lets what
    runs finish, then the container is recreated.
- **Your own Claude** ([docs/people.md](docs/people.md)). Anyone who can watch the farm hatches one Claude, once:
  the browser that hatched it is signed in to it (the others see titles, never prompts, results or conversations).
  When hatching you pick its **skin** (hat, colours, accessory), **APPROVE EVERY MISSION**, and **ALL TOOLS** or the
  tools one by one. Change any of it later in its SETTINGS.
  - **Sign in from your phone:** say "farm login" (or `/farm-login`) to your Claude in the Claude app; it runs
    `clodfarm pair` and gives you a one-time link (and a code for MY CLAUDE on another device).
  - **Tools you turn off are denied at every call** (a PreToolUse hook, so it holds with `bypassPermissions`), and a
    change applies at the next tool call. `clodfarm ...` commands always work.
- **Missions wait for your OK.** Work other Claudes, the planner, Slack or MCP send to a Claude whose person approves
  every mission waits as *pending*, and so do their messages. Its person sees **N TO APPROVE** on the farm (and a push
  on their phone with an ntfy topic in SETTINGS), with APPROVE / DENY. A no, or a day without an answer, goes back to
  whoever asked. `clodfarm approvals | approve | deny` for the manager.
- **The planner** ([docs/planner.md](docs/planner.md)). A goal and a switch (`clodfarm planner goal "..."`,
  `clodfarm planner on`, or the manager panel): it runs in cycles all the time, keeps a notebook, looks at every
  Claude's budget and tools, delegates, builds the tools the goal needs, and rests between cycles. The scarecrow on
  the farm.
- **The farm manager.** The farm password is now the manager's (`clodfarm manager-passwd`). The manager panel (the
  gear, G) switches the planner, makes the farm **private** with a viewer password (a public farm, the default,
  shows the farm and task titles to anyone who reaches it), limits hatching, and signs a person out everywhere.
  `clodfarm farm private|public|hatch-open|hatch-closed` from a shell.
- **The browser is a Claude's tool:** each profile belongs to one Claude, only that Claude gets its tools and only its
  person sees it; without a Claude of your own there is no browser to see. No more default profile: a person adds
  their Claude's (up to 2 per Claude, 16 per box: `FARM_BROWSER_PER_CLAUDE`, `FARM_BROWSER_MAX`), the manager
  assigns (`clodfarm browser assign <profile> <claude>`). A profile nobody owns is the farm's own Claude's.
- **A hundred Claudes, a crowd watching.** The farm grows with its Claudes (pan and zoom, a ROSTER of every Claude),
  the TASKS page filters, searches and pages on the server, and the state is built once a second for everyone
  (with ETags): 300 viewers polling a 100-Claude farm get answers in ~15 ms (p95 ~120 ms) on a laptop.
- **Tokens burned, top left:** every Claude's input, output and cache tokens, in total and today, counted from every
  sub-agent run and every conversation. An owner also sees their own Claude's, with its 5-hour and weekly usage.

## Unreleased (folded into 1.0.0)

- **Schedules say when they run in plain words.** `30 7 * * 1-5` in Jerusalem now reads "weekdays at 7:30am,
  Jerusalem time", and the next run reads "next tomorrow at 12:30 AM your time, in 6h 10m" (on TASKS, in
  `clodfarm schedule list` and over MCP). An unusual cron line still shows as cron; a row's details show the raw line.
- **Dashboard folders.** Put dashboards in folders, nested with "/" (e.g. `Growth/Leads`): the list page shows folder
  tiles you open and a breadcrumb back up; MOVE on a card files it (a new name makes the folder) and RENAME FOLDER
  renames one with everything under it. From a shell: `--folder` on `dashboard push` / `metric`,
  `clodfarm dashboard move <name> <folder>` and `clodfarm dashboard rename-folder <old> <new>`; over MCP, `folder`
  on `farm_dashboard_push`. A push without a folder leaves the dashboard where it is.

## 0.10.0 (2026-09-28)

A TASKS page to see and manage every sub-agent and schedule, and a toolbar that says what each button is.

- **A TASKS page: every sub-agent and schedule, and what you can do to them.** The new clipboard button on the farm
  (or J) opens `/tasks`:
  - **At work and waiting:** every running, waiting and queued sub-agent, whose it is and which Claude runs it.
    Open one to see its instructions and its result so far; CANCEL stops it (it asks first).
  - **Schedules:** when each one runs next, in your time; RUN NOW, PAUSE, RESUME and REMOVE; + SCHEDULE adds one
    (cron, every or once at, in your time zone, on any Claude).
  - **Finished in the last day**, with RETRY; a filter by Claude; and PAUSE THE FARM / RESUME at the top.
  - **From a shell:** `clodfarm schedule pause|resume|run ID` too. A paused schedule doesn't fire; resumed, it runs
    at its next time from now, not the ones it missed.
- **The toolbar says what each button is.** Hovering (or tabbing to) a button shows its name on a little wooden sign,
  with its shortcut key: TALK TO YOUR CLAUDE · T, CONNECT SLACK · S, DASHBOARDS AND STATS · D, TASKS AND
  SCHEDULES · J, THE FARM'S BROWSER · B, ADD A CLAUDE OR A BOT · C.

## 0.9.0 (2026-09-28)

Bots on other models, a proxy for the farm's browser, a Claude you can talk to while its sub-agents work, and a
cancel that stops them.

- **The farm's browser page no longer comes up blank behind a proxy.** The page loads noVNC's ~55 modules at
  once, and a proxy in front (like clod.farm's) opens a connection for each: the UI server's listen backlog of 5 made
  Linux drop most of them, the proxy answered 502, and a module that fails to load stops the whole page (no profiles,
  no buttons, "…" at the top). The server now queues 256 connections, and the page loads noVNC only when it shows a
  screen, so its controls work even if the screen can't load (it then says to reload).
- **Bots: other models on the farm** (#9). A bot is Claude Code on another model, through any provider that speaks
  Anthropic's API: OpenRouter's free models, a local Ollama, a LiteLLM gateway. Add one in the farm UI (+ NEW CLAUDE,
  BOT: OTHER MODEL) or with `clodfarm bot add`: the farm asks the model for one word first and keeps the bot only if
  it answers. A bot uses no Claude account's usage. It takes only the sub-agents sent to it (`--on <bot>`, or any if
  you say so), its own sub-agents stay on it, and it pauses when its provider rate-limits it. The guide tells your
  Claudes to send it well-specified, low-risk jobs and check the result. Its key stays in its own config dir.
- **Talk to your Claude while its sub-agents work.** In a conversation from the Claude app, a Claude used to wait for
  each sub-agent with `clodfarm result <id> --wait` in the foreground: one tool call that blocked for up to 10 minutes,
  so everything you typed waited until that sub-agent finished. The guide (and `clodfarm spawn`'s output) now has it
  wait in the background and end its turn: your next prompt is answered at once, and Claude Code wakes it with the
  result when the sub-agent is done. Checked against the real Claude Code: a prompt sent while a sub-agent ran was
  answered in 2 s, and the conversation woke by itself with the result.
- **Cancel stops a running sub-agent.** It used to only change the task's status: the run went on, used budget and
  could still land its work on main. Now its box notices within 3 s and stops the run, nothing it did lands (also
  when the cancel comes while its check runs), a cancel isn't counted toward the circuit breaker, and cancelling a
  sub-agent cancels the ones under it.
- **A new farm UI password works at once.** `clodfarm ui-passwd` used to take effect only after a restart (the
  running UI kept the old password and sessions); now the UI picks the new one up, and signs old sessions out,
  immediately.
- **What you ask your Claude to tell a running sub-agent is your instruction.** A `clodfarm msg <id>` from a
  conversation to one of its own Claude's sub-agents is shown to it as its person's instruction, not as another
  Claude's request; `--urgent` says so too. Messages from other Claudes and from sub-agents stay requests.
- **A proxy for the farm's browser, from the country you pick.** A CONNECTION bar at the top of `/browser` sends
  each profile DIRECT or VIA PROXY, and says what sites see. The farm has one proxy address: SET PROXY takes
  `LOGIN:PASSWORD@HOST:PORT` (DataImpulse: `gw.dataimpulse.com`, a sticky port 10000–20000 for sites you're logged
  in to), and each profile picks its own country (DataImpulse's `__cr.<country>`, added to the login for it). Every
  switch is checked first and shows the exit IP and city. Chromium can't take a proxy login, so a relay on
  127.0.0.1 adds it; a wrong login is an error on the page, never a login prompt the Claudes would get stuck on.
  WebRTC can't go around the proxy, and a profile with the proxy on never falls back to going direct.
  `FARM_BROWSER_PROXY` sets the address instead; `clodfarm browser proxy on|off [PROFILE] [--country us]`.

## 0.8.0 (2026-09-27)

The farm's browser, a Claude that works in a conversation shows it on the farm, and a farm that stays bright.

- **The farm's browser.** One Chromium in the container that you see and use in the farm UI (the globe button, or
  B, at `/browser`): log in to a site there once (LinkedIn, an admin panel) and every Claude works in that login with
  its new `browser` MCP tools (Playwright attached over DevTools), in the window you watch. START turns it on for the
  box and it stays on across restarts; the profile lives in the workspace volume, so a new container is still logged
  in. The screen comes over the UI's own WebSocket behind the farm password (noVNC); paste and copy work, and ⌘
  shortcuts on a Mac. The guide tells the Claudes to use their own tab, never type passwords (they ask you to log in
  instead) and act on your accounts only as asked. **Profiles**: each its own Chromium with its own logins (two
  LinkedIn accounts: two profiles), added with + PROFILE, up to 8; any Claude can use any of them
  (`mcp__browser__*` for `default`, `mcp__browser-<name>__*` for the others).
  `clodfarm browser [start|stop [PROFILE] | open URL [--profile P] | add|remove NAME]`. The image gains
  Chromium, Xvfb, x11vnc, noVNC and Node; `--build-arg BROWSER=0` builds without them, `FARM_BROWSER=0` turns it off.
- **A Claude at work in a conversation shows it.** While a conversation's turn runs (you asked it something from
  the Claude app, in any session, new or old), its Claude gets a plot, grows crops and works at its laptop, with
  "TALKING: <the conversation>" over its head, like a Claude with sub-agents. The farm's hooks mark a conversation
  busy from its prompt until its turn ends (kept busy while a Stop keeps it going for mail); a turn whose transcript
  goes quiet for 10 minutes (interrupted, so no Stop came) counts as over. `/api/state` gives each Claude `talking`.
- **No night.** The farm no longer darkens in the evening: the blue night wash and the fireflies are gone.
- **Node and the AWS CLI on every box.** The image has Node 22 (npm, npx) and the AWS CLI v2 whatever the build
  args (Node came only with the browser before, and `aws` was downloaded at startup only with the apps role on). `aws`
  has no credentials unless you give the box some (the apps role does).
- **The guide explains the dashboards.** Every Claude's guide now has the dashboard spec and its widgets, says the
  page's link goes to the person, and tells them to use the farm's dashboards instead of building their own.

## 0.7.0 (2026-09-27)

Dashboards that show what is improving, each Claude's tools on its card, and an optional AWS role so the farm can
build and run its own apps.

- **Dashboards.** The Claudes build pages that show what is improving, at `/dashboards/<name>`, with a list at
  `/dashboards` (the chart button on the farm, or D). A dashboard is a JSON spec of widgets (stats, charts, bar lists,
  tables, progress, notes) that the farm draws in its own look; no agent-written code runs in the browser. Every push
  records each stat's value (one point per hour, kept 400 days), so stats show their trend and change over 24h, 7d,
  30d or 90d without the agent keeping any history. `clodfarm dashboard metric` logs one number; `push --run CMD
  --every 1h` makes a dashboard live: the farm runs that code in the repo on schedule (claimed once across boxes) and
  pushes what it prints. When a live dashboard breaks, the page says so and its Claude gets a message (`--wake`) to
  fix it. MCP: `farm_dashboards` and `farm_dashboard_push`.
- **Each Claude's tools.** Tap a Claude: its card lists what it can use, as Claude Code reported it on its last run:
  model and version, MCP servers and whether they're connected (or need sign-in, or failed), built-in and MCP tools,
  skills, plugins and sub-agent types.
- **A smaller toolbar.** Slack is now just its logo, and "talk to your Claude" is a small button (or T) that opens a
  window with the steps, example asks and the MCP command for Claude Code on your computer.
- **Optional apps role: the farm can build and run its own apps on AWS.** `deploy/aws/deploy.sh apps-role --email
  you@example.com [--budget 50] [--domain apps.example.com] [--regions ...]` deploys `deploy/aws/apps-role.yaml`
  (in its own account with `APPS_PROFILE`), lets the farm box assume it and restarts the farm with `FARM_AWS_APPS_*`.
  Every Claude then gets `aws --profile apps` and an AWS section in its guide. Fenced: serverless services only, a
  permissions boundary on every role it creates, no IAM users, keys, email or domain purchases, and an AWS Budget that
  locks the role at 100%. `apps-down` removes it. The plain deployment is unchanged and still gives no AWS access.
- The image is unchanged: with the apps role on, the farm installs the AWS CLI v2 into the farm user's home at startup.
- **The README in the clod.farm theme**: the site's farm scene as the hero, farm-sign section headings, the four steps
  as farm panels, a fresh screenshot of the new UI, and real icons (Slack, Claude and MCP marks, line icons) instead of
  emoji. `scripts/render-readme.sh` regenerates the images.

## 0.6.0 (2026-09-27)

Messages between the Claudes arrive in seconds instead of at the person's next prompt.

- **Claude Code's own messaging on the farm.** The guide teaches `ListAgents` and `SendMessage`: a message reaches a
  live session at its next tool call and wakes an idle one. Every Claude added in the farm UI lists its sessions
  with the farm's own Claude's (`FARM_SHARE_SESSIONS`), so they all see each other, and each takes messages without
  holding them for an approval nobody can give (`crossSessionInbound: accept`, unless set by hand). Sub-agent
  sessions are named `[clodfarm] <claude> · <title> · <id>`, and messages sent this way show up in the event log.
- **`clodfarm msg` to a sub-agent, and delivered while it works.** Address a Claude or a sub-agent id. A doorbell in
  the store tells the recipient's box at once; a `PostToolBatch` hook hands the mail over after its next batch of
  tool calls, the `Stop` hook before its turn ends, and an async hook (`asyncRewake`) wakes an idle conversation. A
  sub-agent that isn't running gets its mail with its prompt. Each message is delivered exactly once (it used to be
  possible twice), has an id and a reply address, and `clodfarm inbox` in a sub-agent reads its own mail.
- **`--urgent`** interrupts a running sub-agent and hands the message over at once: sub-agents now read stream-json on
  an open stdin (`FARM_LIVE_STDIN=0` goes back to a plain prompt).
- **`--wake`**: mail nobody read after `FARM_MAIL_WAKE_AFTER` seconds starts a sub-agent on that Claude's account, or
  resumes the finished sub-agent it was for. Capped by `FARM_MAIL_MAX_HOPS` and `FARM_MAIL_WAKES_PER_HOUR`, so two
  Claudes can't keep each other busy.

## 0.5.0 (2026-09-27)

- **Connect Claude Code on your computer: the farm is a remote MCP server** at `/mcp` (Streamable HTTP).
  `claude mcp add --transport http farm <url>/mcp`, then `/mcp` to sign in on the farm's own page (its password and a
  name for your computer). 15 tools: see the farm, spawn/cancel/retry sub-agents, message Claudes, read their
  answers, schedules. OAuth 2.1 with discovery (RFC 9728/8414), dynamic registration, PKCE S256, audience-bound
  hour-long tokens and rotating refresh tokens stored only as hashes; read-only or read-and-work. It can never log
  Claudes in or out, release them or pause the farm. `clodfarm connect | connections | disconnect ID`;
  `FARM_PUBLIC_URL` behind a proxy that rewrites Host. See docs/mcp.md.
- **Give the farm work from Slack.** DM the farm's app or @mention it in a channel: a sub-agent does the job and
  answers in the thread (👀 while it works, ✅ or ❌ when done). Follow-ups in the thread carry the conversation so
  far; `status` shows the Claudes and their usage. It runs on the sender's own Claude (matched by account
  email, stored only as the seat hash), else on a random Claude with room; `gil: …` runs it on gil's account. Connect it from the farm UI's
  new **SLACK** button in about two minutes: a prefilled Slack app manifest, then two tokens pasted back. It uses
  Socket Mode, so there's no public URL or open port, and the client is standard library only. Only full members of
  the workspace can use it (never guests or people from other organisations), optionally narrowed to an allow list.
  `clodfarm slack` prints the steps for a setup without the UI. See [docs/slack.md](docs/slack.md).
- `clodfarm msg` reaches connected Claude Codes by their name.
- **A 10-second demo at the top of the README** (`assets/demo-10s.webp`, click for the MP4), drawn with the farm UI's
  own sprites. Re-render it with `scripts/render-demo.mjs` (headless Chromium + ffmpeg); `scripts/demo10.html` is the
  deterministic timeline.

## 0.4.4 (2026-09-27)

- **Always the newest Claude Code.** The image ships the `latest` release instead of `stable`, and the farm updates
  it every hour (`FARM_CLAUDE_UPDATE`, 0 = never), so new models such as Opus 5.5 reach every Claude the day they ship.
  On `stable` (2.1.274), `opus` still meant Opus 5. Sub-agents run on a new version at once; each Claude's Remote
  Control restarts on it once no one has talked to that Claude for 15 minutes.
- **The sessions you open from the Claude app use `FARM_MODEL`** by default, like the farm's sub-agents: it is set
  as each Claude's default model.

## 0.4.3 (2026-09-27)

- The farm's own usage check is no longer recorded as a session, and `clodfarm sessions` hides the ones older
  versions recorded (`--all` shows them).

## 0.4.2 (2026-09-26)

- **Usage reads like Claude's own usage page:** "35% used", a bar that fills up to 100% (green, then amber, then red),
  and when it resets. Everywhere: the Claude card, `clodfarm agents`, the guide and the docs.
- **The farm's own Claude is named after its account,** not after the farm: on the farm `jestr`, the Claude logged in
  as matan@… is `matan` (set `FARM_CLAUDE_NAME` to choose). Its Remote Control session is `[clodfarm] jestr · matan`.
  Added Claudes keep the name you give them.

## 0.4.1 (2026-09-26)

- **Every Claude session and its whole conversation are in the farm's store.** A Claude Code hook (`clodfarm hook`,
  installed in each Claude's settings) runs on session start, each prompt, each reply and session end: it registers
  the session (which Claude, conversation or sub-agent, its task, its Remote Control session) and copies the new
  transcript turns (your messages, Claude's replies, tool calls and results in short; thinking is left out). Read
  them with `clodfarm sessions` / `clodfarm session ID`, or on each Claude's card in the farm UI. Messages from other
  Claudes are delivered at the next real prompt (never used up by a session that gets none).
- **Every farm session is marked `[clodfarm]`** in the Claude app and claude.ai/code: the Remote Control session
  (`[clodfarm] <name>`), the sessions you open from it, and each sub-agent (`[clodfarm] <name> · <job>`).

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
