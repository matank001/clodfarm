"""Connectors: accounts the farm connects once, for every Claude on it (the CONNECTORS menu in the farm UI).

Slack lives in slack.py (it talks to the farm). Stripe is here: the farm manager gives the farm a Stripe API key (a
restricted key is best: only the permissions the Claudes need), the farm checks it with Stripe and keeps it in
``.farm/connectors/stripe.json`` (readable by the farm's user only; the UI never shows it again, only its last 4
characters), and every Claude gets Stripe's own MCP server (https://mcp.stripe.com, authorized with that key) as its
``mcp__stripe__*`` tools. A Claude's person can turn Stripe off for their Claude (its tools, "Stripe").
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

STRIPE_MCP = "https://mcp.stripe.com"
STRIPE_NAME = "stripe"  # its MCP server in each Claude's config: mcp__stripe__*
KEY_RE = re.compile(r"^(sk|rk)_(test|live)_[A-Za-z0-9]{10,250}$")


def _api() -> str:
    return (os.environ.get("FARM_STRIPE_API") or "https://api.stripe.com").rstrip("/")  # a stand-in, in tests


def _path(workspace: str) -> str:
    return os.path.join(workspace, ".farm", "connectors", "stripe.json")


def stripe_load(workspace: str) -> dict | None:
    try:
        with open(_path(workspace)) as f:
            d = json.load(f)
        return d if d.get("key") else None
    except (OSError, ValueError):
        return None


def stripe_view(workspace: str) -> dict:
    """What the UI shows: never the key."""
    d = stripe_load(workspace)
    if not d:
        return {"connected": False}
    return {"connected": True, "mode": d.get("mode"), "kind": d.get("kind"), "last4": d["key"][-4:],
            "account": d.get("account") or {}, "by": d.get("by"), "at": d.get("at"), "tools": f"mcp__{STRIPE_NAME}__*"}


def _get(path: str, key: str, timeout: float = 15) -> tuple[int, dict]:
    req = urllib.request.Request(_api() + path, headers={"Authorization": f"Bearer {key}",
                                                          "User-Agent": "clodfarm", "Stripe-Version": "2024-06-20"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (OSError, ValueError) as e:
        raise ValueError(f"couldn't reach Stripe ({e})") from None


def stripe_check(key: str) -> dict:
    """Ask Stripe who this key belongs to. A restricted key may not read the account: then its balance tells that
    the key works. Raises ValueError with what's wrong."""
    key = (key or "").strip()
    m = KEY_RE.match(key)
    if not m:
        raise ValueError("that isn't a Stripe secret key: it starts with sk_test_, sk_live_, rk_test_ or rk_live_ "
                         "(Stripe dashboard → Developers → API keys)")
    kind, mode = ("restricted" if m.group(1) == "rk" else "secret"), m.group(2)
    code, body = _get("/v1/account", key)
    if code == 200:
        s = body.get("settings", {}).get("dashboard", {}) if isinstance(body.get("settings"), dict) else {}
        account = {"id": body.get("id"), "name": s.get("display_name") or (body.get("business_profile") or {}).get("name")
                   or body.get("email"), "country": body.get("country")}
    elif code in (401,):
        raise ValueError("Stripe says this key is not valid (revoked, or mistyped)")
    else:  # a restricted key without the account permission: its balance still says the key works
        code, body = _get("/v1/balance", key)
        if code != 200:
            msg = ((body.get("error") or {}).get("message") or f"HTTP {code}") if isinstance(body, dict) else f"HTTP {code}"
            raise ValueError(f"Stripe refused this key: {msg}")
        account = {}
    return {"mode": mode, "kind": kind, "account": account}


def stripe_connect(workspace: str, key: str, by: str = "") -> dict:
    info = stripe_check(key)
    path = _path(workspace)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"key": key.strip(), **info, "by": by, "at": time.time()}, f)
    os.replace(tmp, path)
    return stripe_view(workspace)


def stripe_disconnect(workspace: str) -> bool:
    try:
        os.remove(_path(workspace))
        return True
    except OSError:
        return False


# --------------------------------------------------------------------- Blender
# A Blender MCP server (streamable HTTP) that drives a Blender somewhere else: the farm manager gives the farm its URL
# and token once, the farm checks that it answers, keeps them in .farm/connectors/blender.json (the farm's user only)
# and every Claude gets it as its mcp__blender__* tools. The marker header lets the farm tell its own entry from a
# "blender" server set up by hand (that one is left alone).
BLENDER_NAME = "blender"
BLENDER_MARK = {"X-Clodfarm-Connector": BLENDER_NAME}


def _blender_path(workspace: str) -> str:
    return os.path.join(workspace, ".farm", "connectors", "blender.json")


def blender_load(workspace: str) -> dict | None:
    try:
        with open(_blender_path(workspace)) as f:
            d = json.load(f)
        return d if d.get("url") else None
    except (OSError, ValueError):
        return None


def blender_view(workspace: str) -> dict:
    """What the UI and CLI show: never the token."""
    d = blender_load(workspace)
    if not d:
        return {"connected": False}
    return {"connected": True, "url": d["url"], "last4": (d.get("token") or "")[-4:] or None,
            "server": d.get("server") or {}, "by": d.get("by"), "at": d.get("at"), "tools": f"mcp__{BLENDER_NAME}__*"}


def blender_check(url: str, token: str = "") -> dict:
    """Open an MCP session with the server (initialize) and return who it says it is. Raises ValueError."""
    url = (url or "").strip()
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ValueError("that isn't an MCP server URL: it looks like https://host/mcp")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "clodfarm", "version": "1"}}})
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **BLENDER_MARK}
    if token:
        headers["Authorization"] = f"Bearer {token.strip()}"
    req = urllib.request.Request(url, data=body.encode(), method="POST", headers={"User-Agent": "clodfarm", **headers})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            text = r.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ValueError(f"the server refused the token (HTTP {e.code})") from None
        raise ValueError(f"the server answered HTTP {e.code}: is this its MCP endpoint (often /mcp)?") from None
    except OSError as e:
        raise ValueError(f"couldn't reach the server ({e})") from None
    for line in [text] + [ln[5:] for ln in text.splitlines() if ln.startswith("data:")]:  # JSON, or an SSE stream
        try:
            info = (json.loads(line).get("result") or {}).get("serverInfo")
        except (ValueError, AttributeError):
            continue
        if isinstance(info, dict):
            return {"name": info.get("name"), "title": info.get("title"), "version": info.get("version")}
    raise ValueError("the server answered, but not as an MCP server")


def blender_connect(workspace: str, url: str, token: str = "", by: str = "") -> dict:
    server = blender_check(url, token)
    path = _blender_path(workspace)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"url": url.strip(), "token": (token or "").strip(), "server": server, "by": by, "at": time.time()}, f)
    os.replace(path + ".tmp", path)
    return blender_view(workspace)


def blender_disconnect(workspace: str) -> bool:
    try:
        os.remove(_blender_path(workspace))
        return True
    except OSError:
        return False


def mcp_servers(workspace: str) -> dict[str, dict]:
    """The connectors' MCP servers every Claude gets (none when nothing is connected)."""
    out = {}
    d = stripe_load(workspace)
    if d:
        out[STRIPE_NAME] = {"type": "http", "url": os.environ.get("FARM_STRIPE_MCP") or STRIPE_MCP,
                            "headers": {"Authorization": f"Bearer {d['key']}"}}
    b = blender_load(workspace)
    if b:
        out[BLENDER_NAME] = {"type": "http", "url": b["url"], "headers": {
            **({"Authorization": f"Bearer {b['token']}"} if b.get("token") else {}), **BLENDER_MARK}}
    return out


def is_ours(name: str, server: dict) -> bool:
    if not isinstance(server, dict) or server.get("type") != "http":
        return False
    if name == BLENDER_NAME:
        return (server.get("headers") or {}).get("X-Clodfarm-Connector") == BLENDER_NAME
    return name == STRIPE_NAME and \
        str(server.get("url", "")).rstrip("/") in (STRIPE_MCP, (os.environ.get("FARM_STRIPE_MCP") or STRIPE_MCP).rstrip("/"))


GUIDE = """
## Stripe (a connector)
The farm is connected to a Stripe account ({mode} mode{acct}): your `mcp__stripe__*` tools act on it (customers,
products, prices, payment links, invoices, subscriptions, balances, the docs). Read and build freely in test mode. In
live mode you work with real money: create charges, refunds, payouts or cancellations, or delete anything, only when
your person explicitly asks for that one thing. Never print or copy the key.
"""


BLENDER_GUIDE = """
## Blender (a connector)
The farm is connected to a Blender MCP server{server}: your `mcp__blender__*` tools drive a Blender that runs there,
not on this box (build scenes and game assets, set materials, animate, render, import and export, run Python in it).
Paths in its tools are on that server. Its own instructions say what it can do: read them before the first call.
"""


def guide_section(workspace: str) -> str:
    """The farm guide's sections for what is connected (Stripe, Blender, Google Ads)."""
    d = stripe_load(workspace)
    name = ((d or {}).get("account") or {}).get("name")
    stripe = GUIDE.format(mode=d.get("mode", "?").upper(), acct=f", {name}" if name else "") if d else ""
    b = blender_load(workspace)
    title = ((b or {}).get("server") or {}).get("title") or ((b or {}).get("server") or {}).get("name")
    blender = BLENDER_GUIDE.format(server=f" ({title})" if title else "") if b else ""
    return stripe + blender + gads_guide(workspace)


# ------------------------------------------------------------------ Google Ads
# The Google Ads API needs an OAuth client (id + secret) in a Cloud project with the Google Ads API enabled (the
# project's access level, Test or Explorer and up, is what Google checks now) and a refresh token for a Google account
# that can see the ad accounts, plus the manager account's ID when working through one (login-customer-id). A
# developer token (the old API Center one) is optional: sent as the developer-token header only when there is one. The farm checks them (refresh token -> access token -> the accounts it can reach),
# keeps them in .farm/connectors/google-ads.json (and a google-ads.yaml for Google's Python library), and every Claude
# uses them through `clodfarm gads` (accounts, GAQL reports, an access token for changes).
GADS_FIELDS = ("client_id", "client_secret", "refresh_token")  # required; developer_token is optional
GADS_OPTIONAL = ("developer_token", "login_customer_id")


def _gads_oauth() -> str:
    return os.environ.get("FARM_GOOGLE_OAUTH") or "https://oauth2.googleapis.com/token"


def _gads_api() -> str:
    return (os.environ.get("FARM_GOOGLE_ADS_API") or "https://googleads.googleapis.com").rstrip("/")


def _gads_versions() -> list[str]:
    return [v.strip() for v in (os.environ.get("FARM_GOOGLE_ADS_VERSIONS") or "v25,v24,v23").split(",") if v.strip()]


def _gads_paths(workspace: str) -> tuple[str, str]:
    d = os.path.join(workspace, ".farm", "connectors")
    return os.path.join(d, "google-ads.json"), os.path.join(d, "google-ads.yaml")


def gads_load(workspace: str) -> dict | None:
    try:
        with open(_gads_paths(workspace)[0]) as f:
            d = json.load(f)
        return d if all(d.get(k) for k in GADS_FIELDS) else None
    except (OSError, ValueError):
        return None


def _digits(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


def _http(method: str, url: str, headers: dict, body: bytes | None = None, timeout: float = 20) -> tuple[int, dict | list]:
    req = urllib.request.Request(url, data=body, method=method, headers={"User-Agent": "clodfarm", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (OSError, ValueError) as e:
        raise ValueError(f"couldn't reach Google ({e})") from None


def _gads_error(body) -> str:
    """The most telling part of a Google Ads API error."""
    err = body[0] if isinstance(body, list) and body else body
    err = (err or {}).get("error") or {} if isinstance(err, dict) else {}
    for d in err.get("details") or []:
        for e in (d.get("errors") or []):
            code = next(iter((e.get("errorCode") or {}).values()), "")
            return f"{code}: {e.get('message', '')}".strip(": ")
    return err.get("message") or err.get("status") or "unknown error"


def gads_access_token(creds: dict) -> str:
    code, body = _http("POST", _gads_oauth(), {"Content-Type": "application/x-www-form-urlencoded"},
                       urllib.parse.urlencode({"client_id": creds["client_id"], "client_secret": creds["client_secret"],
                                               "refresh_token": creds["refresh_token"],
                                               "grant_type": "refresh_token"}).encode())
    if code != 200 or not isinstance(body, dict) or not body.get("access_token"):
        why = ": ".join(str(body[k]) for k in ("error", "error_description") if body.get(k)) \
            if isinstance(body, dict) else ""
        raise ValueError(f"Google refused the OAuth client or refresh token ({why or f'HTTP {code}'})")
    return body["access_token"]


def _gads_headers(creds: dict, token: str) -> dict:
    h = {"Authorization": f"Bearer {token}"}
    if creds.get("developer_token"):
        h["developer-token"] = creds["developer_token"]
    if creds.get("login_customer_id"):
        h["login-customer-id"] = _digits(creds["login_customer_id"])
    return h


def gads_check(creds: dict) -> dict:
    """The accounts these credentials reach, and the API version that answered. Raises ValueError."""
    creds = {k: str(creds.get(k) or "").strip() for k in (*GADS_FIELDS, *GADS_OPTIONAL)}
    missing = [k.replace("_", " ") for k in GADS_FIELDS if not creds[k]]
    if missing:
        raise ValueError("missing: " + ", ".join(missing))
    if creds["login_customer_id"] and len(_digits(creds["login_customer_id"])) != 10:
        raise ValueError("a login customer ID is the manager account's 10 digits (123-456-7890)")
    token = gads_access_token(creds)
    headers = _gads_headers(creds, token)
    for ver in _gads_versions():
        code, body = _http("GET", f"{_gads_api()}/{ver}/customers:listAccessibleCustomers", headers)
        if code == 404:
            continue  # a version Google has retired: try the one before
        if code != 200:
            raise ValueError(f"Google Ads refused the credentials: {_gads_error(body)}")
        ids = [r.split("/")[-1] for r in (body.get("resourceNames") or [])] if isinstance(body, dict) else []
        customers = []
        for cid in ids[:25]:
            name, manager = None, False
            q = json.dumps({"query": "SELECT customer.id, customer.descriptive_name, customer.manager FROM customer"})
            c, b = _http("POST", f"{_gads_api()}/{ver}/customers/{cid}/googleAds:search",
                         {**headers, "Content-Type": "application/json"}, q.encode())
            if c == 200 and isinstance(b, dict) and b.get("results"):
                cu = b["results"][0].get("customer") or {}
                name, manager = cu.get("descriptiveName"), bool(cu.get("manager"))
            customers.append({"id": cid, "name": name, "manager": manager})
        return {"customers": customers, "more": max(0, len(ids) - 25), "api_version": ver,
                "login_customer_id": _digits(creds["login_customer_id"]) or None}
    raise ValueError(f"no Google Ads API version answered ({', '.join(_gads_versions())}); set FARM_GOOGLE_ADS_VERSIONS")


def gads_connect(workspace: str, creds: dict, by: str = "") -> dict:
    info = gads_check(creds)
    js, yml = _gads_paths(workspace)
    os.makedirs(os.path.dirname(js), mode=0o700, exist_ok=True)
    data = {**{k: str(creds.get(k) or "").strip() for k in (*GADS_FIELDS, "developer_token")}, **info, "by": by,
            "at": time.time()}
    yaml = [(k, data[k]) for k in ("developer_token", "client_id", "client_secret", "refresh_token") if data[k]]
    yaml += [("use_proto_plus", True)] + ([("login_customer_id", info["login_customer_id"])]
                                         if info["login_customer_id"] else [])
    for path, text in ((js, json.dumps(data)), (yml, "".join(f"{k}: {json.dumps(v)}\n" for k, v in yaml))):
        fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(path + ".tmp", path)
    return gads_view(workspace)


def gads_view(workspace: str) -> dict:
    d = gads_load(workspace)
    if not d:
        return {"connected": False}
    return {"connected": True, "customers": d.get("customers") or [], "more": d.get("more", 0),
            "login_customer_id": d.get("login_customer_id"), "api_version": d.get("api_version"),
            "developer_token_last4": (d.get("developer_token") or "")[-4:] or None, "client_id": d["client_id"], "by": d.get("by"),
            "at": d.get("at"), "yaml": _gads_paths(workspace)[1]}


def gads_disconnect(workspace: str) -> bool:
    gone = False
    for p in _gads_paths(workspace):
        try:
            os.remove(p)
            gone = True
        except OSError:
            pass
    return gone


def gads_query(workspace: str, customer: str, gaql: str) -> list[dict]:
    """A GAQL report (searchStream): every row, as Google returns them."""
    d = gads_load(workspace)
    if not d:
        raise ValueError("Google Ads isn't connected (the farm UI's CONNECTORS)")
    headers = {**_gads_headers(d, gads_access_token(d)), "Content-Type": "application/json"}
    code, body = _http("POST", f"{_gads_api()}/{d.get('api_version') or _gads_versions()[0]}/customers/"
                       f"{_digits(customer)}/googleAds:searchStream", headers, json.dumps({"query": gaql}).encode(),
                       timeout=120)
    if code != 200:
        raise ValueError(f"Google Ads: {_gads_error(body)}")
    return [r for chunk in (body if isinstance(body, list) else [body]) for r in (chunk.get("results") or [])]


GADS_GUIDE = """
## Google Ads (a connector)
The farm is connected to Google Ads ({n} account(s){names}). Use `clodfarm gads`:
- `clodfarm gads accounts` lists the ad accounts; `clodfarm gads query "<GAQL>" --customer <id>` runs a report
  (e.g. `SELECT campaign.name, metrics.impressions, metrics.clicks, metrics.cost_micros FROM campaign WHERE
  segments.date DURING LAST_7_DAYS`), as JSON rows.
- `clodfarm gads dashboard --customer <id> [--days 30]` prints a ready dashboard (spend, clicks, conversions, CPA,
  per day and per campaign). Make it live: `clodfarm dashboard push ads --run "clodfarm gads dashboard --customer
  <id>" --every 1h`, or write your own GAQL into a dashboard script that calls `clodfarm gads query`.
- For changes (budgets, bids, pausing, new campaigns) use the REST API with `clodfarm gads token` (its headers and
  base URL), or Google's Python library with GOOGLE_ADS_CONFIGURATION_FILE_PATH={yaml}.
Reports are free to run. Changes spend real money: make them only when your person asks for that change, and
say what you changed. Never print or copy the tokens.
"""


def gads_guide(workspace: str) -> str:
    d = gads_load(workspace)
    if not d:
        return ""
    names = ", ".join(c.get("name") or c["id"] for c in (d.get("customers") or [])[:5])
    return GADS_GUIDE.format(n=len(d.get("customers") or []), names=f": {names}" if names else "",
                             yaml=_gads_paths(workspace)[1])


def _n(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def gads_dashboard(workspace: str, customer: str, days: int = 30) -> dict:
    """A dashboard spec (dashboards.py) of one ad account over the last ``days`` days: spend, clicks, impressions,
    conversions and their ratios as stats (so the farm keeps their history), spend and clicks per day, and campaigns.
    For a live dashboard: `clodfarm dashboard push ads --run "clodfarm gads dashboard --customer ID" --every 1h`."""
    days = max(1, min(int(days), 365))
    cid = _digits(customer)
    end = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))  # yesterday: today's numbers are still moving
    start = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400 * days))
    during = f"segments.date BETWEEN '{start}' AND '{end}'"
    metrics = "metrics.cost_micros, metrics.clicks, metrics.impressions, metrics.conversions"
    acct = (gads_query(workspace, cid, "SELECT customer.descriptive_name, customer.currency_code FROM customer") or
            [{}])[0].get("customer") or {}
    cur = acct.get("currencyCode") or ""
    daily: dict[str, list[float]] = {}
    for r in gads_query(workspace, cid, f"SELECT segments.date, {metrics} FROM customer WHERE {during}"):
        m, d = r.get("metrics") or {}, (r.get("segments") or {}).get("date")
        if d:
            row = daily.setdefault(d, [0.0, 0.0, 0.0, 0.0])
            for i, k in enumerate(("costMicros", "clicks", "impressions", "conversions")):
                row[i] += _n(m.get(k))
    camps: dict[str, dict] = {}
    for r in gads_query(workspace, cid, f"SELECT campaign.id, campaign.name, campaign.status, {metrics} FROM campaign "
                                        f"WHERE {during}"):
        c, m = r.get("campaign") or {}, r.get("metrics") or {}
        it = camps.setdefault(str(c.get("id") or c.get("name")), {"name": c.get("name") or "?", "status": c.get(
            "status") or "", "cost": 0.0, "clicks": 0.0, "impr": 0.0, "conv": 0.0})
        it["cost"] += _n(m.get("costMicros")) / 1e6
        it["clicks"] += _n(m.get("clicks"))
        it["impr"] += _n(m.get("impressions"))
        it["conv"] += _n(m.get("conversions"))
    cost = sum(v[0] for v in daily.values()) / 1e6
    clicks, impr, conv = (sum(v[i] for v in daily.values()) for i in (1, 2, 3))
    r2 = lambda x: round(x, 2)  # noqa: E731
    per = f"{days} days"
    widgets = [
        {"type": "stat", "key": "cost", "label": f"Spend ({per})", "value": r2(cost), "unit": cur, "good": "down"},
        {"type": "stat", "key": "clicks", "label": f"Clicks ({per})", "value": clicks, "good": "up"},
        {"type": "stat", "key": "impressions", "label": f"Impressions ({per})", "value": impr, "good": "up"},
        {"type": "stat", "key": "conversions", "label": f"Conversions ({per})", "value": r2(conv), "good": "up"},
        {"type": "stat", "key": "ctr", "label": "CTR", "value": r2(100 * clicks / impr) if impr else None, "unit": "%",
         "good": "up"},
        {"type": "stat", "key": "cpc", "label": "Cost per click", "value": r2(cost / clicks) if clicks else None,
         "unit": cur, "good": "down"},
        {"type": "stat", "key": "cpa", "label": "Cost per conversion", "value": r2(cost / conv) if conv else None,
         "unit": cur, "good": "down"},
        {"type": "chart", "label": "Spend per day", "unit": cur, "width": "half",
         "series": [{"name": "spend", "points": [[d, r2(v[0] / 1e6)] for d, v in sorted(daily.items())]}]},
        {"type": "chart", "label": "Clicks and conversions per day", "width": "half",
         "series": [{"name": "clicks", "points": [[d, v[1]] for d, v in sorted(daily.items())]},
                    {"name": "conversions", "points": [[d, r2(v[3])] for d, v in sorted(daily.items())]}]},
    ]
    top = sorted(camps.values(), key=lambda c: -c["cost"])
    if top:
        widgets += [
            {"type": "bars", "label": f"Spend by campaign ({per})", "unit": cur,
             "items": [{"label": c["name"], "value": r2(c["cost"])} for c in top[:15]]},
            {"type": "table", "label": "Campaigns", "columns": ["campaign", "status", "spend", "clicks", "impressions",
                                                                "conversions", "CPA"],
             "rows": [[c["name"], c["status"].lower(), r2(c["cost"]), c["clicks"], c["impr"], r2(c["conv"]),
                       r2(c["cost"] / c["conv"]) if c["conv"] else None] for c in top[:200]]},
        ]
    name = acct.get("descriptiveName") or cid
    return {"title": f"Google Ads · {name}",
            "description": f"Account {cid[:3]}-{cid[3:6]}-{cid[6:]}, {start} to {end}" + (f", in {cur}" if cur else ""),
            "widgets": widgets}
