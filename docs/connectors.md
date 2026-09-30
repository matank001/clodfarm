# Connectors

Accounts the farm connects once, for every Claude on it. The **CONNECTORS** button on the farm (or S) opens them.

| Connector | What it gives the farm |
|---|---|
| **Slack** | People give the farm work from Slack; a sub-agent answers in the thread ([slack.md](slack.md)). |
| **Stripe** | Every Claude can use the farm's Stripe account: customers, products, prices, payment links, invoices, subscriptions, balances, and Stripe's docs. |
| **Blender** | Every Claude can drive a Blender that runs on another machine, through its MCP server: scenes, game assets, materials, animation, renders, import and export, Python. |
| **Google Ads** | Every Claude can see the farm's ad accounts, run reports (GAQL), keep live dashboards of them, and make changes when its person asks. |

## Stripe

The farm manager connects it: **CONNECTORS → STRIPE**, paste a key, **CONNECT**. From the box's shell:
`clodfarm stripe connect` (the key on stdin, so it stays out of your shell history), `clodfarm stripe`,
`clodfarm stripe disconnect`.

- **Which key.** Best: a **restricted key** (Stripe → Developers → API keys → Create restricted key) with only
  what the Claudes need, for example write access to Products, Prices, Payment Links and Customers, read access to
  the rest. A secret key works too. Start with a **test-mode** key (`sk_test_…` / `rk_test_…`): nothing real
  happens. A live key (`…_live_…`) means real money; the panel says so.
- **How the Claudes use it.** The farm checks the key with Stripe, keeps it in `/workspace/.farm/connectors/stripe.json`
  (readable by the farm's user only; the UI shows only its last 4 characters), and gives every Claude Stripe's own
  MCP server (`https://mcp.stripe.com`, authorized with that key) as its `mcp__stripe__*` tools. Their farm guide
  says: build freely in test mode; in live mode, charge, refund, pay out, cancel or delete only when their person
  asks for that one thing.
- **Per Claude.** A person can turn Stripe off for their own Claude: its SETTINGS → tools → *Stripe (payments)*.
- **Disconnect** removes the key and every Claude's Stripe tools. Revoke the key in Stripe too if it may have leaked.

The Claudes share one container: a Claude with a shell could read the key file. Give the farm a restricted key with
only the permissions you're happy for any of its Claudes to use ([security.md](security.md)).

## Blender

The farm manager connects a Blender MCP server that runs elsewhere (a streamable-HTTP MCP endpoint, often ending in
`/mcp`): **CONNECTORS → BLENDER**, its URL and token, **CONNECT**. From the box's shell:
`clodfarm blender connect https://host/mcp` (its token on stdin, so it stays out of your
shell history; an empty line for a server without one), `clodfarm blender`, `clodfarm blender disconnect`.

- **How the Claudes use it.** The farm opens an MCP session with the server to check it answers, keeps the URL and
  token in `/workspace/.farm/connectors/blender.json` (readable by the farm's user only; only the token's last 4
  characters are shown, and only the manager sees where the server runs), and gives every Claude the server as its
  `mcp__blender__*` tools. Their farm guide says the Blender runs there, not on the farm's box, so paths in its
  tools are on that server.
- **Per Claude.** A person can turn Blender off for their own Claude: its SETTINGS → tools → *Blender (3D)*.
- **Your own server.** A `blender` MCP server you set up by hand in a Claude's config is left alone.
- **Disconnect** removes the URL, the token and every Claude's Blender tools.

The Claudes share one container: a Claude with a shell could read the token. Such a server usually runs Python in
Blender, so connect only a server whose token you're happy for any of the farm's Claudes to use.

## Google Ads

Google Ads has no single key: the API wants three things, and a fourth when you go through a manager account.

| Field | Where it comes from |
|---|---|
| **OAuth client ID + secret** | Google Cloud console → APIs & Services → Credentials → Create OAuth client ID (*Desktop app*), in a project with the **Google Ads API** enabled. The project's **access level** is what Google checks: a new one has *Test* (test accounts only); on the Google Ads API page, Access levels → Manage, apply for *Explorer* to reach real accounts (usually granted in minutes). |
| **Refresh token** | Sign in once, as a Google user who can see the ad accounts, with scope `https://www.googleapis.com/auth/adwords` (Google's `generate_user_credentials.py` example, or the OAuth Playground with your own client). |
| **Login customer ID** (optional) | The manager account's ID (123-456-7890), when the ad accounts are reached through it. |
| **Developer token** (optional) | Only if you have one from a manager account's API Center (Google's older access model). The farm sends it as the `developer-token` header when it's there. |

The farm manager connects it: **CONNECTORS → GOOGLE ADS**, paste the fields, **CONNECT**. The panel then lists the
ad accounts (and a developer token's last 4 characters, if there is one), never the secrets. Or from the box's shell:

```bash
clodfarm gads connect          # asks for each field (the secrets aren't echoed)
clodfarm gads connect < creds.json   # or: {"client_id": ..., "client_secret": ..., "refresh_token": ...,
                                     #      "login_customer_id": "1234567890"}   (+ "developer_token", optional)
clodfarm gads                  # status
clodfarm gads disconnect
```

On AWS, run them in the container (`docker exec -i clodfarm clodfarm gads connect < creds.json` over SSM). The farm
turns the refresh token into an access token, asks Google Ads which accounts it reaches (trying the newest API
version first), and lists them.

- **How the Claudes use it.** The credentials go in `/workspace/.farm/connectors/google-ads.json`, plus a
  `google-ads.yaml` for Google's own Python library (both readable by the farm's user only). Every Claude's farm guide
  teaches `clodfarm gads`:
  - `clodfarm gads accounts [--refresh]`: the ad accounts;
  - `clodfarm gads query "SELECT campaign.name, metrics.clicks FROM campaign WHERE segments.date DURING
    LAST_7_DAYS" --customer <id>`: a report, as JSON rows (the query first, then `--customer`);
  - `clodfarm gads dashboard --customer <id> [--days 30]`: a ready dashboard of the account (below);
  - `clodfarm gads token`: a fresh access token with the headers and base URL, for REST calls that change things.

  Reports cost nothing. Changes spend money, so the guide says to make them only when the Claude's person asks for
  that change.
- **Dashboards.** `clodfarm gads dashboard` prints a [dashboard](dashboards.md) of one account over the last 30 days
  (`--days` changes it): spend, clicks, impressions, conversions, CTR, cost per click and per conversion as stats (so
  the farm keeps their history and trends), spend and clicks per day, and spend per campaign. Make it live, and the
  farm refreshes it every hour:

  ```bash
  clodfarm dashboard push ads --folder Growth --run "clodfarm gads dashboard --customer 2345678901" --every 1h
  ```

  For other views, a Claude writes a dashboard script in the repo that calls `clodfarm gads query` with its own GAQL.
- **API versions.** Google retires a Google Ads API version about a year after it ships. The farm tries
  `v25,v24,v23` in that order; set `FARM_GOOGLE_ADS_VERSIONS` to change the list.
- **Disconnect** removes both files. To cut access for good, revoke the refresh token too (the Google account's
  security page → third-party access).

Anyone with these credentials can do what the signed-in Google user can do in those ad accounts. Use a Google user
with only the access the Claudes need (Google Ads → Access and security → a *Standard* or *Read only* role).
