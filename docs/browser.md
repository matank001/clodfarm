# The farm's browser

Chromium runs in the farm's container. You see it and use it in the farm UI (the globe button, or `B`, at
`/browser`), and every Claude drives it with its browser MCP tools. Log in to a site there once (LinkedIn, an
admin panel, a dashboard behind SSO) and the Claudes work in that login, in the same window you watch.

It has **profiles**: each one its own Chromium with its own logins, so two LinkedIn accounts are two profiles. Every
profile is shared by everyone on the box, and any Claude can use any of them. The first one is `default`.

## Log in to a site for the Claudes

1. Open the farm UI and click the globe (or press `B`).
2. **START THE BROWSER** the first time. It stays on, across restarts, until someone presses **STOP**.
3. Type an address in the bar (`linkedin.com/login`) or click into the screen and use Chromium's own address bar.
4. Log in as you normally would, including two-factor codes and captchas. The Claudes never type passwords.
5. Tell your Claude what to do there ("go through my LinkedIn notifications and summarize them"; with more than one
   profile, say which: "in the linkedin-work profile"). It opens its own tab; the tab list under the screen shows
   what's open.

## Profiles

**+ PROFILE** adds one (a name like `linkedin-work`: lowercase letters, digits and `-`), up to 8. Each profile's tab
shows a green dot while it runs; START and STOP work per profile, and a running profile takes about 300 MB of memory.
**REMOVE PROFILE** stops it and deletes its logins (the `default` profile can only be stopped). Every Claude gets the
new profile's tools in its next session.

Paste with ⌘V / Ctrl+V into the screen. What you copy there (⌘C / Ctrl+C) lands on your own clipboard. On a Mac,
⌘A, ⌘C, ⌘X, ⌘Z, ⌘F and ⌘L work as they do at home (Chromium in the container gets Ctrl).

## A proxy (DataImpulse and others)

A profile can go out through a proxy, so the sites it visits see the proxy's address (a residential one, in a
country you pick) instead of your cloud box's. The farm has one proxy address; each profile goes **DIRECT** or
**VIA PROXY**, from its own country.

The **CONNECTION** bar at the top of `/browser`, under the profile tabs, is for the selected profile:

1. Click **VIA PROXY** (or **SET PROXY**), paste the address as `LOGIN:PASSWORD@HOST:PORT` and click
   **CHECK & TURN ON**. The farm logs in to the proxy, keeps it only if that works, and sends the profile through it.
   A running profile restarts: its tabs close, its logins stay.
2. Pick the country in the menu next to the switch (DataImpulse). Each profile has its own, so a US account and a
   German one each show their own country.
3. The bar says what sites see: *this box's own IP address*, or *a proxy IP in United States* with the last check's
   IP and city. Every switch to the proxy (and every new country) is checked first; a check that fails changes nothing.

Turn the proxy on *before* you log in to a site, so the site never sees the box's address.

For **DataImpulse**, the login and password are on your plan's page in the DataImpulse dashboard, and the host is
`gw.dataimpulse.com`. The country menu adds `__cr.<country>` to the login for you. Choose the port, and put other
options in the login yourself (after `__`, separated by `;`):

| You want | Address |
|---|---|
| the same IP for about 30 minutes (for sites you're logged in to) | `LOGIN:PASSWORD@gw.dataimpulse.com:10000` (any port 10000–20000: the IP is tied to the port) |
| that IP kept for 60 minutes | `LOGIN__sessttl.60:PASSWORD@gw.dataimpulse.com:10000` |
| a new IP on every request (not for logins: sites see you jump around) | `LOGIN:PASSWORD@gw.dataimpulse.com:823` |
| no login, with this box's IP whitelisted in the dashboard | `gw.dataimpulse.com:10000` |

`http://LOGIN:PASSWORD@HOST:PORT` and `HOST:PORT:LOGIN:PASSWORD` work too, and so does any other provider's HTTP
proxy. SOCKS5 does not: Chromium can't log in to a SOCKS5 proxy (DataImpulse serves HTTP on the same ports). Every
profile with the proxy on shares that one address, so with a sticky port they share its IP too.

How it works: Chromium's `--proxy-server` can't carry a login, and its login prompt would stop the Claudes. So each
proxied profile's Chromium goes to a small relay the farm runs on `127.0.0.1`, which adds the login and passes
everything on to the proxy. WebRTC can't go around the proxy, and `localhost` stays direct. If the proxy refuses the
login, the page shows an error and the CONNECTION bar says so, instead of a login prompt. A profile with
the proxy on never falls back to going direct: if the proxy is removed, the profile stops until you set a proxy again
or turn it off.

`FARM_BROWSER_PROXY` in `.env` sets the address instead, and then the UI can't change it. The address from the UI is
kept in `/workspace/.farm/browser-proxy.json`, readable by the farm's user only.
`clodfarm browser proxy on|off [PROFILE] [--country us]` does the same from a shell. The country menu is there for
DataImpulse only; with another provider, put the country in its login the way that provider documents.

## What the Claudes get

Every Claude on the box (the farm's own and each one added in the UI) gets one MCP server per profile:
[Playwright MCP](https://github.com/microsoft/playwright-mcp) attached to that profile's Chromium over its DevTools
port. The `default` profile's server is `browser` (`playwright-mcp --cdp-endpoint http://127.0.0.1:9222`, tools
`mcp__browser__browser_navigate`, `_snapshot`, `_click`, `_type`, `_take_screenshot`, `_tabs`, ...); a profile named
`linkedin-work` is `browser-linkedin-work` (`mcp__browser-linkedin-work__*`). They show on each Claude's card under
**TOOLS**. An MCP server of the same name you set up yourself is left alone. Their screenshots and logs go to
`/workspace/.farm/browser-files/<profile>`, not into the Claude's worktree.

The farm guide tells them: open your own tab and close it when done, don't log out or change account settings, never
type passwords or one-time codes (ask the person to log in from the UI instead), post, message, buy or delete only
when asked, and go at a human pace. They use the profile of the account the job is about, and ask when they can't tell. When a profile is off they
can start it (`clodfarm browser start <profile>`); they don't add or remove profiles.

```bash
clodfarm browser                          # every profile: on or off, its tools and its tabs (--json)
clodfarm browser start|stop [PROFILE]     # for the whole box (default: default); the logins are kept
clodfarm browser open linkedin.com/feed [--profile P]
clodfarm browser add NAME | remove NAME   # remove deletes that profile's logins
clodfarm browser proxy on|off [PROFILE] [--country us]   # through the farm's proxy (set in the UI), or direct
```

## How it works

| Piece | Where |
|---|---|
| Chromium | one per profile, each on its own virtual screen (Xvfb `:99`, `:100`, ...); logins in `/workspace/.farm/browser` (`default`) and `/workspace/.farm/browsers/<name>`: the workspace volume, so a new container is still logged in |
| DevTools | `127.0.0.1:9222` for profile slot 0, `9223` for slot 1, ...; inside the container only |
| Screen | x11vnc on `127.0.0.1:5900`, `5901`, ...; inside the container only |
| Viewer | noVNC in the farm UI, over the UI's own WebSocket `/api/browser/screen?profile=<name>`: it needs the UI's login cookie and a page of the farm (the `Origin` is checked) |

The farm UI's process keeps it running (restarts a crashed Chromium, backs off when it keeps crashing) and stops it
cleanly with the farm, so Chromium saves its cookies. `/workspace/.farm/browsers.json` lists the profiles, their
slots and which should run; `/workspace/.farm/browser.log` (and `browsers/<name>.log`) has their output.

## Settings

| Variable | Default | |
|---|---|---|
| `FARM_BROWSER` | `1` | `0` turns it off: no browser page, no MCP server for the Claudes |
| `FARM_BROWSER_SIZE` | `1280x800` | the screen (and window) size |
| `FARM_BROWSER_LANG` | `en-US` | Chromium's language |
| `FARM_BROWSER_HOME` | `about:blank` | the page it opens on start |
| `FARM_BROWSER_ARGS` | | more Chromium flags |
| `FARM_BROWSER_PROFILE` | `/workspace/.farm/browser` | the `default` profile's directory |
| `FARM_BROWSER_PROXY` | | the proxy, `LOGIN:PASSWORD@HOST:PORT` (instead of SET PROXY in the UI); each profile still turns it on |

The image has it unless built with `docker build --build-arg BROWSER=0 .` (Chromium, Xvfb, x11vnc, noVNC and Playwright
add about 1 GB unpacked; Node is in every image). Each box of a multi-box farm has its own browser and its own logins.

## Security

Whoever has the farm UI password can use every site you log in to here, and so can every Claude on the box: that's
the point. Log in only to accounts you want the farm to act on, prefer a separate browser account over your main
one where a site allows it, and log out (or **REMOVE PROFILE**) to take access back.
Chromium runs with `--no-sandbox`: the container is the sandbox, as for the agents (see [security.md](security.md)).
Sites may limit automated use of an account (LinkedIn does); what the Claudes do there is done as you.
